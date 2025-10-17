import os
import re
import json
import nltk
import time
import uuid
import base64
import fitz
import pdfplumber
from io import BytesIO
from tqdm import tqdm
from pathlib import Path
from rapidfuzz import fuzz
from typing import List, Dict, Any
from transformers import AutoTokenizer
from PIL import Image, UnidentifiedImageError
from collections import defaultdict, Counter
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from concurrent.futures import as_completed, ProcessPoolExecutor
from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.document_converter import DocumentConverter, PdfFormatOption, WordFormatOption

from llm_utils import filter_with_llm, classify_text_with_llm, summarize_table, translate_text_with_llm
from vision_utils import generate_image_summary


nltk.download("punkt")
nltk.download('punkt_tab')


IMAGE_RESOLUTION_SCALE = 1.0
excluded_labels = {
    'page_header', 'page_footer', 'caption', 'reference'
}


class TocPageMapper:
    def __init__(self, doc_path: str):
        """Initialize and extract the TOC from the document."""
        self.doc_path = doc_path
        self.doc = fitz.open(doc_path)
        self.toc = self.doc.get_toc()  # Get the TOC
        self.page_mapping = {}

    def extract_text(self, page_number: int) -> str:
        """Extract text from a given page number."""
        page = self.doc.load_page(page_number)
        return page.get_text("text")
    
    def match_toc_to_pages(self, threshold=80):
        """
        Try to match TOC entries to actual PDF pages based on text similarity.
        This method assumes that TOC entries correspond to section headers.
        """
        for toc_entry in self.toc:
            toc_page = toc_entry[-1]  # TOC page number (1-based index)
            toc_title = toc_entry[1].strip()  # Title of the TOC entry
            
            # Extract text from the actual PDF page and compare it with TOC title
            for actual_page in range(toc_page - 1, len(self.doc)):  # Start search from the TOC page onward
                page_text = self.extract_text(actual_page)
                score = fuzz.partial_ratio(toc_title.lower(), page_text.lower())  # Use fuzzy matching

                if score >= threshold:
                    self.page_mapping[toc_page] = actual_page + 1  # Save the mapping (1-based index)
                    # print(f"Mapped TOC entry {'#'*toc_entry[0]} {toc_title} to PDF page {actual_page + 1} (Score: {score})")
                    break  # Stop once a match is found

    def get_page_mapping(self):
        """Return the page mapping."""
        return self.page_mapping

    def close(self):
        """Close the document."""
        self.doc.close()



class TocHeaders:
    """Compute data for identifying header text based on the Table Of Contents (TOC).

    This class uses the document's TOC to identify headers on pages. The header
    identification is based on TOC entries which have hierarchical levels. This
    is a more accurate and faster method for documents with a well-defined TOC.
    """

    def __init__(self, doc: str, page_mapping=None):
        """
        Read and store the TOC of the document. Optionally, use a page_mapping to adjust TOC pages to
        the corresponding PDF page numbers if they don't align (e.g., prefatory pages not numbered in TOC).
        
        :param page_mapping: A dictionary that maps TOC page numbers to actual PDF page numbers.
        """
        if isinstance(doc, fitz.Document):
            self.mydoc = doc
        else:
            self.mydoc = fitz.open(doc)

        self.TOC = self.mydoc.get_toc()
        if not self.TOC:
            raise ValueError("No TOC found in the document.")
        
        # If a page_mapping is provided, use it to adjust TOC page numbers
        self.page_mapping = page_mapping if page_mapping else {}

    def get_header_id(self, span: dict, page=None, threshold=90) -> str:
        """Return appropriate markdown header prefix based on TOC.

        Given a text span from an extraction (e.g., from pdfplumber or another tool),
        determine the markdown header prefix string of `#` characters based on the TOC
        entries, adjusting for page number discrepancies if necessary.
        """
        
        # Adjust for possible mismatch between TOC page and actual PDF page number
        adjusted_page = self._get_adjusted_page_number(page)
        if adjusted_page is None:
            return ""
        
        # Retrieve the TOC entries that match the adjusted page number
        my_toc = [t for t in self.TOC if adjusted_page - 1 <= t[-1] <= adjusted_page + 1]
        if not my_toc:  # no TOC items present on this page
            return ""
        
        # Check if the span matches a TOC entry
        text = span["text"].strip()  # remove leading and trailing whitespace
        for t in my_toc:
            title = t[1].strip()  # title of TOC entry
            lvl = t[0]  # level of TOC entry (e.g., 1 for top-level, 2 for subsections)
            
            # Match text and title
            score = fuzz.partial_ratio(title.lower(), text.lower())
            if score >= threshold:
                print(f'Heading: {title}, Level: {lvl}, Score: {score}')
                return "#" * lvl  # Return corresponding markdown header
        
        return ""  # No match found for this page/span combination

    def _get_adjusted_page_number(self, page):
        """Adjust the PDF page number using the page_mapping if provided."""
        if self.page_mapping:
            # Check if this page number needs to be adjusted based on the TOC
            for toc_page, actual_page in self.page_mapping.items():
                if page == actual_page:
                    return toc_page
        return page  # If no adjustment is needed, return the actual page number

    def close(self):
        """Close the document when done."""
        self.mydoc.close()


def find_text_font_size(
    pdf_path: str,
    search_string: str,
    page_number: int = 0,
    fuzz_threshold: float = 80,
    exact_match_first: bool = False
) -> List[Dict[str, Any]]:
    """ Searches for text in a PDF page and returns font info and bbox for fuzzy-matching lines. """
    matches = []

    try:
        with pdfplumber.open(pdf_path) as pdf:
            if page_number >= len(pdf.pages):
                print(f"Page {page_number} does not exist in PDF.")
                return []

            page = pdf.pages[page_number]
            words = page.extract_words(extra_attrs=["size", "fontname"])

            if not words:
                print("No words found on page.")
                return []

            # Group words into lines based on Y-coordinate
            lines_dict = defaultdict(list)
            for word in words:
                if not all(k in word for k in ("text", "top", "x0", "x1", "bottom", "size", "fontname")):
                    continue  # skip incomplete word entries
                top_key = round(word["top"], 1)
                lines_dict[top_key].append(word)

            for line_words in lines_dict.values():
                sorted_line = sorted(line_words, key=lambda w: w["x0"])
                line_text = " ".join(w["text"] for w in sorted_line)

                # Try exact match if enabled
                if exact_match_first and search_string.lower() == line_text.lower():
                    score = 100
                else:
                    score = fuzz.partial_ratio(line_text.lower(), search_string.lower())

                if score >= fuzz_threshold:
                    font_sizes = [w["size"] for w in sorted_line if w["size"] is not None]
                    font_names = [w["fontname"] for w in sorted_line if w["fontname"]]

                    # Most common font size and name as representative
                    font_size = Counter(font_sizes).most_common(1)[0][0] if font_sizes else None
                    font_name = Counter(font_names).most_common(1)[0][0] if font_names else None

                    x0 = min(w["x0"] for w in sorted_line)
                    x1 = max(w["x1"] for w in sorted_line)
                    top = min(w["top"] for w in sorted_line)
                    bottom = max(w["bottom"] for w in sorted_line)

                    matches.append({
                        "matched_text": line_text,
                        "match_score": score,
                        "font_size": font_size,
                        "font_name": font_name,
                        "bbox": (x0, top, x1, bottom)
                    })

    except Exception as e:
        print(f"Error processing PDF: {e}")

    return matches


def process_document(res, pdf_path, out_path, gen_model, gen_endpoint, vlm_model, vlm_endpoint, vlm_hosting_type, start_time, timings):
    doc_json = res.document.export_to_dict()
    stem = res.input.file.stem

    # Initialize TocHeaders to get the Table of Contents (TOC)
    toc_headers = None
    try:
        toc_mapper = TocPageMapper(pdf_path)
        toc_mapper.match_toc_to_pages(threshold=80)
        page_mapping = toc_mapper.get_page_mapping()
        print("Page Mapping:", page_mapping)
        toc_mapper.close()
        toc_headers = TocHeaders(pdf_path, page_mapping)
    except Exception as e:
        print(f"No TOC found or failed to load TOC: {e}")

    # --- Text Extraction ---
    t0 = time.time()
    filtered_blocks, image_captions, table_captions = [], [], []
    for block in doc_json.get('texts', []):
        block_type = block.get('label', '')
        if block_type not in excluded_labels:
            filtered_blocks.append(block)
        if block_type == 'caption':
            block_parent = block.get('parent', {}).get('$ref', '')
            if 'tables' in block_parent:
                table_captions.append(block)
            elif 'pictures' in block_parent:
                image_captions.append(block)
    timings['extract_text_blocks'] = time.time() - t0

    if len(filtered_blocks):

        # t0 = time.time()
        # filtered_text_dicts = filter_with_llm(filtered_blocks, gen_model, gen_endpoint)
        # (Path(out_path) / f"{stem}_filtered_text.json").write_text(json.dumps(filtered_text_dicts, indent=2), encoding="utf-8")
        # timings['llm_filter_text'] = time.time() - t0

        filtered_text_dicts = filtered_blocks

        structured_output = []

        last_header_level = 0  # To track the last header level in case we don't find it in TOC

        t0 = time.time()
        for text_obj in tqdm(filtered_text_dicts, desc="Document Structure Extraction"):
            label = text_obj.get("label", "")

            # Check if it's a section header and process TOC or fallback to font size extraction
            if label == "section_header":
                prov_list = text_obj.get("prov", [])

                for prov in prov_list:
                    page_no = prov.get("page_no")
                    bbox_dict = prov.get("bbox")

                    if page_no is None or bbox_dict is None:
                        continue

                    # Use TocHeaders to get the markdown header prefix from TOC if available
                    if toc_headers:
                        header_prefix = toc_headers.get_header_id({"text": text_obj.get("text", "")}, page_no)
                        if header_prefix:
                            # If TOC matches, use the level from TOC
                            structured_output.append({
                                "label": label,
                                "text": f"{header_prefix} {text_obj.get("text", "")}",
                                "page": page_no,
                                "font_size": None,  # Font size isn't necessary if TOC matches
                            })
                            last_header_level = len(header_prefix.strip())  # Update last header level
                        else:
                            # If no match, use the previous header level + 1
                            new_header_level = last_header_level + 1
                            structured_output.append({
                                "label": label,
                                "text": f"{'#' * new_header_level} {text_obj.get("text", "")}",
                                "page": page_no,
                                "font_size": None,  # Font size isn't necessary if TOC matches
                            })
                    else:
                        # Fallback to font size extraction if no TOC match is found
                        matches = find_text_font_size(pdf_path, text_obj.get("text", ""), page_no - 1)

                        if len(matches):
                            font_size = 0
                            count = 0
                            for match in matches:
                                font_size += match["font_size"] if match["match_score"] == 100 else 0
                                count += 1 if match["match_score"] == 100 else 0
                            font_size = font_size / count if count else None

                            structured_output.append({
                                "label": label,
                                "text": text_obj.get("text", ""),
                                "page": page_no,
                                "font_size": round(font_size, 2) if font_size else None
                            })
            else:
                structured_output.append({
                    "label": label,
                    "text": text_obj.get("text", ""),
                    "page": text_obj.get("prov")[0].get("page_no"),
                    "font_size": None
                })

        timings["font_size_extraction"] = time.time() - t0

        (Path(out_path) / f"{stem}_clean_text.json").write_text(json.dumps(structured_output, indent=2), encoding="utf-8")
        
    else:
        (Path(out_path) / f"{stem}_clean_text.json").write_text(json.dumps(filtered_blocks, indent=2), encoding="utf-8")

    if toc_headers:
        toc_headers.close()  # Close the TOC handler when done processing

    # --- Image Extraction ---
    if len(doc_json.get('pictures', [])):
        t0 = time.time()
        image_dict, image_uris, ordered_image_captions = [], [], []
        for image_idx, block in enumerate(doc_json.get('pictures', [])):
            caption = ''
            for child in block.get('children', []):
                child_id = child['$ref']
                for caption_idx, child_block in enumerate(image_captions):
                    if child_block.get('self_ref', '') == child_id:
                        caption += f'{child_block["text"]} '
                        image_captions.pop(caption_idx)
                        break
            uri = block.get('image', {}).get('uri', '')
            image_uris.append(uri)
            ordered_image_captions.append(caption)
        timings['extract_images'] = time.time() - t0

        t0 = time.time()
        image_summaries = generate_image_summary(list(zip(image_uris, ordered_image_captions)), vlm_model, vlm_endpoint, vlm_hosting_type)
        timings['generate_image_summaries'] = time.time() - t0

        t0 = time.time()
        for idx, (uri, summary, caption) in enumerate(zip(image_uris, image_summaries, ordered_image_captions)):
            image_dict.append({idx: {'image': uri, 'caption': caption, 'summary': summary}})
        decisions = classify_text_with_llm(image_summaries, gen_model, gen_endpoint)
        filtered_image_dicts = [image_dict[idx] for idx, keep in enumerate(decisions) if keep]
        (Path(out_path) / f"{stem}_images.json").write_text(json.dumps(filtered_image_dicts, indent=2), encoding="utf-8")
        timings['filter_image_summaries'] = time.time() - t0
    else:
        (Path(out_path) / f"{stem}_images.json").write_text(json.dumps([], indent=2), encoding="utf-8")

    # --- Table Extraction ---
    if len(res.document.tables):
        t0 = time.time()
        table_htmls_dict = {}
        table_captions_dict = {i: None for i in range(len(res.document.tables))}
        for table_ix, table in enumerate(res.document.tables):
            table_htmls_dict[table_ix] = table.export_to_html(doc=res.document)
            for caption_idx, block in enumerate(table_captions):
                if block.get('parent')['$ref'] == f'#/tables/{table_ix}':
                    table_captions_dict[table_ix] = block.get('text', '')
                    table_captions.pop(caption_idx)
                    break
        table_htmls = [table_htmls_dict[key] for key in sorted(table_htmls_dict)]
        table_captions_list = [table_captions_dict[key] for key in sorted(table_captions_dict)]
        timings['extract_tables'] = time.time() - t0

        t0 = time.time()
        table_summaries = summarize_table(table_htmls, table_captions_list, gen_model, gen_endpoint)
        timings['summarize_tables'] = time.time() - t0

        t0 = time.time()
        decisions = classify_text_with_llm(table_summaries, gen_model, gen_endpoint)
        filtered_table_dicts = {
            idx: {
                'html': html,
                'caption': caption,
                'summary': summary
            }
            for idx, (keep, html, caption, summary) in enumerate(zip(decisions, table_htmls, table_captions_list, table_summaries)) if keep
        }
        (Path(out_path) / f"{stem}_tables.json").write_text(json.dumps(filtered_table_dicts, indent=2), encoding="utf-8")
        timings['filter_tables'] = time.time() - t0
    else:
        (Path(out_path) / f"{stem}_tables.json").write_text(json.dumps([], indent=2), encoding="utf-8")

    total_time = time.time() - start_time
    print(f"\n[Timing for {stem}] Total: {total_time:.2f}s")
    for k, v in timings.items():
        print(f"  {k:<30}: {v:.2f}s")



def convert_and_process(path, doc_converter, out_path, llm_model, llm_endpoint, vlm_model, vlm_endpoint, vlm_hosting_type):
    try:
        start_time = time.time()
        timings = {}
        t0 = time.time()
        res = doc_converter.convert(path)
        timings['conversion_time'] = time.time() - t0
        process_document(res, path, out_path, llm_model, llm_endpoint, vlm_model, vlm_endpoint, vlm_hosting_type, start_time, timings)
    except Exception as e:
        print(f"Error converting or processing {path}: {e}")


def extract_document_data(input_paths, out_path, llm_model, llm_endpoint, vlm_model, vlm_endpoint, vlm_hosting_type, force=False):
    # Accelerator & pipeline options
    accelerator_options = AcceleratorOptions(num_threads=8, device=AcceleratorDevice.MPS)
    pipeline_options = PdfPipelineOptions()
    pipeline_options.accelerator_options = accelerator_options
    pipeline_options.do_ocr = True
    pipeline_options.do_table_structure = True
    pipeline_options.table_structure_options.do_cell_matching = True
    pipeline_options.images_scale = IMAGE_RESOLUTION_SCALE
    pipeline_options.generate_picture_images = True

    # Skip files that already exist
    filtered_input_paths = [
        path for path in input_paths if force or not (
            (Path(out_path) / f"{Path(path).stem}_clean_text.json").exists() and
            (Path(out_path) / f"{Path(path).stem}_images.json").exists() and
            (Path(out_path) / f"{Path(path).stem}_tables.json").exists()
        )
    ]
    print(f"Processing {len(filtered_input_paths)} files...")

    doc_converter = DocumentConverter(
        allowed_formats=[
            InputFormat.PDF, InputFormat.IMAGE, InputFormat.DOCX, InputFormat.HTML,
            InputFormat.PPTX, InputFormat.ASCIIDOC, InputFormat.CSV, InputFormat.MD,
            # InputFormat.ODT,
        ],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )

    if filtered_input_paths:
        with ProcessPoolExecutor(max_workers=max(1, min(4, len(filtered_input_paths)))) as executor:
            futures = [
                executor.submit(convert_and_process, path, doc_converter, out_path, llm_model, llm_endpoint, vlm_model, vlm_endpoint, vlm_hosting_type)
                for path in input_paths
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    print(f"Unhandled exception: {e}")
    else:
        print("No files to process.")

def collect_header_font_sizes(elements):
    """
    elements: list of dicts with at least keys: 'label', 'font_size'
    Returns a sorted list of unique section_header font sizes, descending.
    """
    sizes = {
        el['font_size']
        for el in elements
        if el.get('label') == 'section_header' and el.get('font_size') is not None
    }
    return sorted(sizes, reverse=True)

def get_header_level(text, font_size, sorted_font_sizes):
    """
    Determine header level based on markdown syntax or font size hierarchy.
    """
    text = text.strip()

    # Priority 1: Markdown syntax
    if text.startswith('#'):
        level = len(text.strip()) - len(text.strip().lstrip('#'))
        return level, text.strip().lstrip('#').strip()

    # Priority 2: Font size ranking
    try:
        level = sorted_font_sizes.index(font_size) + 1
    except ValueError:
        # Unknown font size → assign lowest priority
        level = len(sorted_font_sizes)

    return level, text


def count_tokens(text, tokenizer):
    return len(tokenizer.encode(text))


def split_text_into_token_chunks(text, tokenizer, max_tokens=512, overlap=50):
    sentences = nltk.sent_tokenize(text)
    chunks = []
    current_chunk = []
    current_token_count = 0

    for sentence in sentences:
        token_len = count_tokens(sentence, tokenizer)

        if current_token_count + token_len > max_tokens:
            # save current chunk
            chunk_text = " ".join(current_chunk)
            chunks.append(chunk_text)
            # overlap logic (optional)
            if overlap > 0 and len(current_chunk) > 0:
                overlap_text = current_chunk[-1]
                current_chunk = [overlap_text]
                current_token_count = count_tokens(sentence, tokenizer)
            else:
                current_chunk = []
                current_token_count = 0

        current_chunk.append(sentence)
        current_token_count += token_len

    # flush last
    if current_chunk:
        chunk_text = " ".join(current_chunk)
        chunks.append(chunk_text)

    return chunks


def flush_chunk(current_chunk, chunks, tokenizer, max_tokens):
    content = current_chunk["content"].strip()
    if not content:
        return

    # Split content into token chunks
    token_chunks = split_text_into_token_chunks(content, tokenizer, max_tokens=max_tokens)

    for i, part in enumerate(token_chunks):
        chunk = {
            "chapter_title": current_chunk["chapter_title"],
            "section_title": current_chunk["section_title"],
            "subsection_title": current_chunk["subsection_title"],
            "subsubsection_title": current_chunk["subsubsection_title"],
            "content": part,
            "page_range": sorted(set(current_chunk["page_range"])),
            "source_nodes": current_chunk["source_nodes"].copy()
        }
        if len(token_chunks) > 1:
            chunk["part_id"] = i + 1
        chunks.append(chunk)

    # Reset current_chunk after flushing
    current_chunk["chapter_title"] = ""
    current_chunk["section_title"] = ""
    current_chunk["subsection_title"] = ""
    current_chunk["subsubsection_title"] = ""
    current_chunk["content"] = ""
    current_chunk["page_range"] = []
    current_chunk["source_nodes"] = []


def process_single_file(input_path, output_path, tokenizer, max_tokens=512):
    print(f"Processing {input_path} -> {output_path}")
    
    if not Path(output_path).exists():
        with open(input_path, "r") as f:
            data = json.load(f)
        
        font_size_levels = collect_header_font_sizes(data)

        chunks = []
        current_chunk = {
            "chapter_title": None,
            "section_title": None,
            "subsection_title": None,
            "subsubsection_title": None,
            "content": "",
            "page_range": [],
            "source_nodes": []
        }

        current_chapter = None
        current_section = None
        current_subsection = None
        current_subsubsection = None

        for idx, block in enumerate(data):
            label = block.get("label")
            text = block.get("text", "").strip()
            try:
                page_no = block.get("prov", {})[0].get("page_no")
            except:
                page_no = 0
            ref = f"#texts/{idx}"

            if label == "section_header":
                level, full_title = get_header_level(text, block.get("font_size"), font_size_levels)
                if level == 1:
                    current_chapter = full_title
                    current_section = None
                    current_subsection = None
                    current_subsubsection = None
                elif level == 2:
                    current_section = full_title
                    current_subsection = None
                    current_subsubsection = None
                elif level == 3:
                    current_subsection = full_title
                    current_subsubsection = None
                else:
                    current_subsubsection = full_title

                # Flush current chunk and update
                flush_chunk(current_chunk, chunks, tokenizer, max_tokens)
                current_chunk["chapter_title"] = current_chapter
                current_chunk["section_title"] = current_section
                current_chunk["subsection_title"] = current_subsection
                current_chunk["subsubsection_title"] = current_subsubsection

            elif label in {"text", "list_item", "code", "formula"}:
                if current_chunk["chapter_title"] is None:
                    current_chunk["chapter_title"] = current_chapter
                if current_chunk["section_title"] is None:
                    current_chunk["section_title"] = current_section
                if current_chunk["subsection_title"] is None:
                    current_chunk["subsection_title"] = current_subsection
                if current_chunk["subsubsection_title"] is None:
                    current_chunk["subsubsection_title"] = current_subsubsection

                if label == 'code':
                    current_chunk["content"] += f"```\n{text}\n``` "
                elif label == 'formula':
                    current_chunk["content"] += f"${text}$ "
                else:
                    current_chunk["content"] += f"{text} "
                if page_no is not None:
                    current_chunk["page_range"].append(page_no)
                current_chunk["source_nodes"].append(ref)
            else:
                print(f'Skipping adding "{label}".')

        # Flush any remaining content
        flush_chunk(current_chunk, chunks, tokenizer, max_tokens)

        # Save the processed chunks to the output file
        with open(output_path, "w") as f:
            json.dump(chunks, f, indent=2)

        print(f"✅ {len(chunks)} RAG chunks saved to {output_path}")
    else:
        print(f"{output_path} already exists.")

def hierarchical_chunk_with_token_split(input_paths, output_paths, max_tokens=512):
    if len(input_paths) != len(output_paths):
        raise ValueError("`input_paths` and `output_paths` must have the same length")

    tokenizer = AutoTokenizer.from_pretrained('ibm-granite/granite-embedding-278m-multilingual')

    # Process each input-output file pair in parallel using ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=8) as executor:
        futures = []
        for input_path, output_path in zip(input_paths, output_paths):
            print(f"Submitting task for: {input_path} -> {output_path}")
            futures.append(executor.submit(process_single_file, input_path, output_path, tokenizer, max_tokens))

        # Wait for all futures to finish and handle exceptions
        for future in futures:
            try:
                future.result()  # Capture exceptions if any
            except Exception as e:
                print(f"Error occurred: {e}")


def create_chunk_documents(in_txt_f, in_img_f, in_tab_f, orig_fn, include_meta_info_in_main_text, collection_name, translation_model, translation_endpoint):

    os.makedirs(f'images_{collection_name}', exist_ok=True)

    with open(in_txt_f, "r") as f:
        txt_data = json.load(f)

    with open(in_img_f, "r") as f:
        img_data = json.load(f)

    with open(in_tab_f, "r") as f:
        tab_data = json.load(f)

    txt_docs = []
    translated_txt_docs = []
    if len(txt_data):
        for txt_id, block in enumerate(txt_data):
            meta_info = ''
            if block.get('chapter_title'):
                meta_info += f"Chapter: {block.get('chapter_title')} "
            if block.get('section_title'):
                meta_info += f"Section: {block.get('section_title')} "
            if block.get('subsection_title'):
                meta_info += f"Subsection: {block.get('subsection_title')} "
            if block.get('subsubsection_title'):
                meta_info += f"Subsubsection: {block.get('subsubsection_title')} "
            txt_docs.append({
                # "chunk_id": txt_id,
                "page_content": f'{meta_info}\n{block.get("content")}' if include_meta_info_in_main_text else block.get("content"),
                "filename": orig_fn,
                "type": "text",
                "source": meta_info,
                "language": "en"
            })
        
        text_chunks = [doc["page_content"] for doc in txt_docs]
        translated_text_chunks = translate_text_with_llm(text_chunks, translation_model, translation_endpoint, target_lang="Hindi")
        for translation, orig_chunk in zip(translated_text_chunks, txt_docs):
            translated_txt_docs.append({
                "page_content": translation,
                "filename": orig_chunk["filename"],
                "type": "text",
                "source": orig_chunk["source"],
                "language": "hi"
            })

    img_docs = []
    translated_img_docs = []
    if len(img_data):
        for img_id, block in enumerate(img_data):
            block = list(block.values())[0]
            img_path = f"images_{collection_name}/{uuid.uuid5(uuid.uuid5(uuid.NAMESPACE_DNS, collection_name), f'{orig_fn.strip().lower()}_img_{str(img_id).strip()}').hex}.png"

            uri = block.get('image')
            # Split off the header if present
            if ',' in uri:
                _, b64_data = uri.split(',', 1)
            else:
                b64_data = uri
            try:
                # Decode base64 string safely
                img_data = base64.b64decode(b64_data)
                # Load the image using PIL
                image = Image.open(BytesIO(img_data))
                image.load()  # Ensure the image is fully loaded
                image.save(img_path)
                # img_docs.append(Document(
                # page_content=block.get('summary'),
                #     metadata={"filename": orig_fn, "type": "image", "source": img_path, "chunk_id": img_id}
                # ))
                img_docs.append({
                    "page_content": block.get("summary"),
                    "filename": orig_fn,
                    "type": "image",
                    "source": img_path,
                    "language": "en"
                })
            except base64.binascii.Error:
                print("❌ Error: The base64 data is invalid.")
            except UnidentifiedImageError:
                print("❌ Error: Cannot identify image file. The data might not be a valid image.")
            except Exception as e:
                print(f"❌ Unexpected error: {e}")
        
        img_chunks = [doc["page_content"] for doc in img_docs]
        translated_img_chunks = translate_text_with_llm(img_chunks, translation_model, translation_endpoint, target_lang="Hindi")
        for translation, orig_chunk in zip(translated_img_chunks, img_docs):
            translated_img_docs.append({
                "page_content": translation,
                "filename": orig_chunk["filename"],
                "type": "image",
                "source": orig_chunk["source"],
                "language": "hi"
            })
    
    tab_docs = []
    translated_tab_docs = []
    if len(tab_data):
        tab_data = list(tab_data.values())
        for tab_id, block in enumerate(tab_data):
            # tab_docs.append(Document(
            #     page_content=block.get('summary'),
            #     metadata={"filename": orig_fn, "type": "table", "source": block.get('html'), "chunk_id": tab_id}
            # ))
            tab_docs.append({
                "page_content": block.get("summary"),
                "filename": orig_fn,
                "type": "table",
                "source": block.get("html"),
                "language": "en"
            })
        
        tab_chunks = [doc["page_content"] for doc in tab_docs]
        translated_tab_chunks = translate_text_with_llm(tab_chunks, translation_model, translation_endpoint, target_lang="Hindi")
        for translation, orig_chunk in zip(translated_tab_chunks, tab_docs):
            translated_tab_docs.append({
                "page_content": translation,
                "filename": orig_chunk["filename"],
                "type": "table",
                "source": orig_chunk["source"],
                "language": "hi"
            })

    combined_docs = txt_docs + img_docs + tab_docs + translated_txt_docs + translated_img_docs + translated_tab_docs

    stats = f'{len(txt_docs)} Text Chunks, {len(img_docs)} Images, and {len(tab_docs)} Tables.\n'
    stats += f'{len(translated_txt_docs)} Translated Text Chunks, {len(translated_img_docs)} Translated Images, and {len(translated_tab_docs)} Translated Tables.'

    return combined_docs, stats
