import json

import requests
import time
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI
from transformers import AutoTokenizer



tokenizer=AutoTokenizer.from_pretrained('ibm-granite/granite-3.3-8b-instruct')

with open("api_key.txt", "r") as file:
    api_key = file.read().strip()


def classify_text_with_llm(text_blocks, gen_model, gen_endpoint, batch_size=128):
    prompt_template = """You are a smart assistant for RAG corpus creation. Your task is to decide whether the following text be included in a technical documentation knowledge base? Respond only with "yes" or "no".

    "yes" for technical content: concepts, configs, commands, behavior, features, etc. that are useful for technical question answering.
    "no" if the content is about a person (author/editor), acknowledgements, contact info, copyright, trademark, foreword, notices, disclaimer, and preface.

    Text: {text}

    Answer:
    """

    all_prompts = [prompt_template.format(text=item.strip()) for item in text_blocks]
    
    decisions = []
    for i in tqdm(range(0, len(all_prompts), batch_size), desc="Classifying Text with LLM"):
        batch_prompts = all_prompts[i:i + batch_size]

        payload = {
            "model": gen_model,
            "prompt": batch_prompts,
            "temperature": 0,
            "max_tokens": 3,
        }
        try:
            response = requests.post(gen_endpoint, json=payload)
            response.raise_for_status()
            result = response.json()
            choices = result.get("choices", [])
            for choice in choices:
                reply = choice.get("text", "").strip().lower()
                decisions.append("yes" in reply)
        except Exception as e:
            print(f"Error in vLLM: {e}")
            decisions.append(True)
    return decisions


def filter_with_llm(text_blocks, gen_model, gen_endpoint):
    text_contents = [block.get('text') for block in text_blocks]

    # Run classification
    decisions = classify_text_with_llm(text_contents, gen_model, gen_endpoint)
    print(f"[Debug] Prompts: {len(text_contents)}, Decisions: {len(decisions)}")
    filtered_blocks = [block for dcsn, block in zip(decisions, text_blocks) if dcsn]
    print(f"[Debug] Filtered Blocks: {len(filtered_blocks)}, True Decisions: {sum(decisions)}")
    return filtered_blocks


def summarize_single_table(prompt, gen_model, gen_endpoint):
    payload = {
        "model": gen_model,
        "prompt": prompt,
        "temperature": 0,
        "repetition_penalty": 1.1,
        "max_tokens": 512,
        "stream": False,
    }
    try:
        response = requests.post(gen_endpoint, json=payload)
        response.raise_for_status()
        result = response.json()
        # try:
        #     reply = result.get("response").strip().lower()
        # except:
        #     reply = result.get("choices", [{}])[0].get("text", "").strip()
        reply = result.get("choices", [{}])[0].get("text", "").strip()
        return reply
    except Exception as e:
        print(f"Error summarizing table: {e}")
        return "No summary."


def summarize_table(table_html, table_caption, gen_model, gen_endpoint, max_workers=32):
    prompt_template = """
You are a smart assistant analyzing agriculture related documents. 
You are given a table extracted from a document. Your task is to summarize the key points and insights from the table. Avoid repeating the entire content; focus on what is meaningful or important.

Table:
{content}

Summary:
"""
    
    all_prompts = [prompt_template.format(content=html) for html in table_html]

    summaries = [None] * len(all_prompts)

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(all_prompts)))) as executor:
        futures = {
            executor.submit(summarize_single_table, prompt, gen_model, gen_endpoint): idx
            for idx, prompt in enumerate(all_prompts)
        }

        for future in tqdm(as_completed(futures), total=len(futures), desc="Summarizing Tables"):
            idx = futures[future]
            try:
                summaries[idx] = future.result()
            except Exception as e:
                print(f"Thread failed at index {idx}: {e}")
                summaries[idx] = "No summary."

    return summaries


def query_vllm(question, documents, endpoint, ckpt, language, stop_words, max_new_tokens, rag=True, stream=False, use_in_context=False, max_input_length=6000, dynamic_chunk_truncation=True):
    template_token_count=250
    context = "\n\n".join([doc.get("page_content") for doc in documents])
    
    print(f'Original Context: {context}')
    if dynamic_chunk_truncation:
        question_token_count=len(tokenizer.encode(question))

        reamining_tokens=max_input_length-(template_token_count+question_token_count)

        context=tokenizer.decode(tokenizer.encode(context)[:reamining_tokens])

        print(f"Truncated Context: {context}")


    # if use_in_context:
    #     prompt_template = """
    #     Answer the question strictly based on the context.
    #     If the context does not answer the question then you continue generating the answer from your prior knowledge.

    #     Context:
    #     {context}

    #     Question:
    #     {question}

    #     Answer:
    #     """ if rag else """
    #     Question:
    #     {question}

    #     Answer:
    #     """
    # else:
    #     prompt_template = """
    #     Answer the question strictly based on the context.
    #     If the context does not answer the question then you continue generating the answer from your prior knowledge.

    #     Context:
    #     {context}

    #     Question:
    #     {question}

    #     Answer:
    #     """ if rag else """
    #     Question:
    #     {question}

    #     Answer:
    #     """
    
    # if language.lower().strip():
    #     question += f"\n\nAnswer the question in {language} language."


    if language == "hi":
        if use_in_context:
            prompt_template = """
आपको मिलेगा:
1. कृषि संदर्भ – तथ्यात्मक जानकारी सहित।
2. किसान का प्रश्न – सलाह हेतु।
उत्तर सरल, सटीक, औचित्यपूर्ण होना चाहिए।
यदि संदर्भ अपर्याप्त हो, तो अपने ज्ञान से उत्तर दें।

Context:
अध्याय: सब्जियों की खेती के लिए प्रथाओं का पैकेज अनुभाग: पंजाब कृषि विश्वविद्यालय, लुधियाना उपअनुभाग: सामग्री उपउपअनुभाग: जलवायु और मिट्टी
लंबी कद्दू एक गर्म मौसम की फसल है। इसे सुरक्षित परिस्थितियों में भी उगाया जा सकता है ताकि पहले उपज प्राप्त की जा सके। यह फसल रेतीली मिट्टी से लेकर भारी मिट्टी तक की एक विस्तृत श्रृंखला में उगाई जा सकती है।

Question:
मेरे खेत में रेतीली मिट्टी है, तो क्या लंबी कद्दू यहाँ अच्छी उगेगी?

Answer:
हाँ, लंबी कद्दू आपके खेत में रेतीली मिट्टी में अच्छी उग सकती है। यह फसल रेतीली मिट्टी से लेकर भारी मिट्टी तक की एक विस्तृत श्रृंखला में उगाई जा सकती है।


Context:
{context}

Question:
{question}

Answer:
"""
        else:
            prompt_template = """
आपको मिलेगा:
1. कृषि संदर्भ – तथ्यात्मक जानकारी सहित।
2. किसान का प्रश्न – सलाह हेतु।
उत्तर सरल, सटीक, औचित्यपूर्ण होना चाहिए।
यदि संदर्भ अपर्याप्त हो, तो अपने ज्ञान से उत्तर दें।


Context:
{context}

Question:
{question}

Answer:
"""
    if language == "en":
        if use_in_context:
            prompt_template = """
You are given:  
1. **A short context of agricultural guidance text** containing factual information.  
2. **A farmer's question** asking for advice. 
3. ** Return a consise, to-the-point answer which is grounded on the context provided strictly.
4. ** Answer the question, strictly in **English**

The answer should be accurate, easy to follow, based on the context(s), and with proper justification.
If the context does not answer the question, answer it based on your prior knowledge.

Context:
Chapter: PACKAGE OF PRACTICES FOR CULTIVATION OF VEGETABLES Section: PUNJAB AGRICULTURAL UNIVERSITY LUDHIANA Subsection: CONTENTS Subsubsection: Climate and Soil Long melon is a warm season crop. It can also be grown under protected conditions to get early yield. The crop can be grown in wide range of soils ranging between sandy laom to heavy soil.

Question:
Bhai, my farm has sandy loam soil and I'm thinking of planting long melon. Will it grow well here?

Answer:
Yes, long melon can grow well in sandy loam soil. According to the guidance from Punjab Agricultural University Ludhiana, long melon is a warm season crop that can be cultivated in a wide range of soils, including sandy loam. Therefore, your farm's soil type is suitable for growing long melon.


Context:
{context}

Question:
{question}

Answer:
"""
        else:
            prompt_template = """
You are given:  
1. **A short context of agricultural guidance text** containing factual information.  
2. **A farmer's question** asking for advice. 
3. ** Return a consise, to-the-point answer which is grounded on the context provided strictly.
4. ** Answer the question, strictly in **English**

The answer should be accurate, easy to follow, based on the context(s), and with proper justification.
If the context does not answer the question, answer it based on your prior knowledge.

Context:
{context}

Question:
{question}

Answer:
"""



    prompt = prompt_template.format(context=context, question=question)
    print("PROMPT:  ", prompt)
    headers = {
        "accept": "application/json",
        "RITS_API_KEY": api_key,
        "Content-type": "application/json"
    }
    payload = {
        # "prompt": prompt,
        "messages": [{"role": "user", "content": prompt}],
        "model": ckpt,
        "max_tokens": max_new_tokens,
        "repetition_penalty": 1.1,
        "temperature": 0.0,
        "stop": stop_words,
        "stream": stream
    }
    
    try:
        start_time = time.time()
        # Use requests for synchronous HTTP requests
        response = requests.post(endpoint, json=payload, headers=headers)
        response.raise_for_status()
        response_data = response.json()
        end_time = time.time()
        request_time = end_time - start_time
        return response_data, request_time
    except Exception as e:
        return {"error": str(e)}, 0.


def query_vllm_stream(question, documents, endpoint, ckpt, language, stop_words, max_new_tokens, rag=True, stream=False,
               use_in_context=False, max_input_length=6000, dynamic_chunk_truncation=True):
    template_token_count = 250
    context = "\n\n".join([doc.get("page_content") for doc in documents])

    print(f'Original Context: {context}')
    if dynamic_chunk_truncation:
        question_token_count = len(tokenizer.encode(question))

        reamining_tokens = max_input_length - (template_token_count + question_token_count)

        context = tokenizer.decode(tokenizer.encode(context)[:reamining_tokens])

        print(f"Truncated Context: {context}")

    # if use_in_context:
    #     prompt_template = """
    #     Answer the question strictly based on the context.
    #     If the context does not answer the question then you continue generating the answer from your prior knowledge.

    #     Context:
    #     {context}

    #     Question:
    #     {question}

    #     Answer:
    #     """ if rag else """
    #     Question:
    #     {question}

    #     Answer:
    #     """
    # else:
    #     prompt_template = """
    #     Answer the question strictly based on the context.
    #     If the context does not answer the question then you continue generating the answer from your prior knowledge.

    #     Context:
    #     {context}

    #     Question:
    #     {question}

    #     Answer:
    #     """ if rag else """
    #     Question:
    #     {question}

    #     Answer:
    #     """

    # if language.lower().strip():
    #     question += f"\n\nAnswer the question in {language} language."

    if language == "hi":
        if use_in_context:
            prompt_template = """
आपको मिलेगा:
1. कृषि संदर्भ – तथ्यात्मक जानकारी सहित।
2. किसान का प्रश्न – सलाह हेतु।
उत्तर सरल, सटीक, औचित्यपूर्ण होना चाहिए।
यदि संदर्भ अपर्याप्त हो, तो अपने ज्ञान से उत्तर दें।

Context:
अध्याय: सब्जियों की खेती के लिए प्रथाओं का पैकेज अनुभाग: पंजाब कृषि विश्वविद्यालय, लुधियाना उपअनुभाग: सामग्री उपउपअनुभाग: जलवायु और मिट्टी
लंबी कद्दू एक गर्म मौसम की फसल है। इसे सुरक्षित परिस्थितियों में भी उगाया जा सकता है ताकि पहले उपज प्राप्त की जा सके। यह फसल रेतीली मिट्टी से लेकर भारी मिट्टी तक की एक विस्तृत श्रृंखला में उगाई जा सकती है।

Question:
मेरे खेत में रेतीली मिट्टी है, तो क्या लंबी कद्दू यहाँ अच्छी उगेगी?

Answer:
हाँ, लंबी कद्दू आपके खेत में रेतीली मिट्टी में अच्छी उग सकती है। यह फसल रेतीली मिट्टी से लेकर भारी मिट्टी तक की एक विस्तृत श्रृंखला में उगाई जा सकती है।


Context:
{context}

Question:
{question}

Answer:
"""
        else:
            prompt_template = """
आपको मिलेगा:
1. कृषि संदर्भ – तथ्यात्मक जानकारी सहित।
2. किसान का प्रश्न – सलाह हेतु।
उत्तर सरल, सटीक, औचित्यपूर्ण होना चाहिए।
यदि संदर्भ अपर्याप्त हो, तो अपने ज्ञान से उत्तर दें।

Context:
{context}

Question:
{question}

Answer:
"""
    if language == "en":
        if use_in_context:
            prompt_template = """
You are given:  
1. **A short context of agricultural guidance text** containing factual information.  
2. **A farmer's question** asking for advice. 
3. ** Return a consise, to-the-point answer which is grounded on the context provided strictly.
4. ** Answer the question, strictly in **English**

The answer should be accurate, easy to follow, based on the context(s), and with proper justification.
If the context does not answer the question, answer it based on your prior knowledge.

Context:
Chapter: PACKAGE OF PRACTICES FOR CULTIVATION OF VEGETABLES Section: PUNJAB AGRICULTURAL UNIVERSITY LUDHIANA Subsection: CONTENTS Subsubsection: Climate and Soil Long melon is a warm season crop. It can also be grown under protected conditions to get early yield. The crop can be grown in wide range of soils ranging between sandy laom to heavy soil.

Question:
Bhai, my farm has sandy loam soil and I'm thinking of planting long melon. Will it grow well here?

Answer:
Yes, long melon can grow well in sandy loam soil. According to the guidance from Punjab Agricultural University Ludhiana, long melon is a warm season crop that can be cultivated in a wide range of soils, including sandy loam. Therefore, your farm's soil type is suitable for growing long melon.


Context:
{context}

Question:
{question}

Answer:
"""
        else:
            prompt_template = """
You are given:  
1. **A short context of agricultural guidance text** containing factual information.  
2. **A farmer's question** asking for advice. 
3. ** Return a consise, to-the-point answer which is grounded on the context provided strictly.
4. ** Answer the question, strictly in **English**

The answer should be accurate, easy to follow, based on the context(s), and with proper justification.
If the context does not answer the question, answer it based on your prior knowledge.

Context:
{context}

Question:
{question}

Answer:
"""

    prompt = prompt_template.format(context=context, question=question)
    print("PROMPT:  ", prompt)
    headers = {
        "accept": "application/json",
        "RITS_API_KEY": api_key,
        "Content-type": "application/json"
    }
    payload = {
        # "prompt": prompt,
        "messages": [{"role": "user", "content": prompt}],
        "model": ckpt,
        "max_tokens": max_new_tokens,
        "repetition_penalty": 1.1,
        "temperature": 0.0,
        "stop": stop_words,
        "stream": stream
    }

    try:
        start_time = time.time()
        # Use requests for synchronous HTTP requests
        print("STREAMING RESPONSE")
        with requests.post(endpoint, json=payload, headers=headers, stream=True) as r:
            for line in r.iter_lines(decode_unicode=True):
                if line:
                    print("Earlier response: ", line)
                    line = line.replace("data: ", "")
                    try:
                        data = json.loads(line)
                        yield data.get("choices", [{}])[0]['delta']['content']
                    except json.JSONDecodeError:
                        print("error in decoding")
                        pass  # ignore malformed lines
    except Exception as e:
        return {"error": str(e)}, 0.


def translate_text_with_llm_helper(text, model_id, client, hosting_type, target_lang="Hindi", retries=3):
    prompt_text = f"""Translate the following text into {target_lang}.
Keep meaning and tone intact, without adding extra commentary.

Text: {text}

Translation:"""

    for attempt in range(retries):
        try:
            if hosting_type == "vllm":
                response = client.chat.completions.create(
                    model=model_id,
                    messages=[
                        {"role": "system", "content": "You are a helpful language translation assistant."},
                        {"role": "user", "content": prompt_text}
                    ],
                    temperature=0.0,
                    max_tokens=2048
                )
                return response.choices[0].message.content.strip()

            else:
                raise ValueError(f"Unsupported hosting type: {hosting_type}")

        except Exception as e:
            print(f"[Attempt {attempt+1}] Error during translation: {e}")
            time.sleep(1)

    return "Translation failed after multiple attempts."


def translate_text_with_llm(text_blocks, gen_model, gen_endpoint, hosting_type="vllm", target_lang="Hindi"):
    if hosting_type == "vllm":
        client = OpenAI(api_key="EMPTY", base_url=gen_endpoint)
    elif hosting_type == "openai":
        client = OpenAI(api_key=gen_endpoint)  # For OpenAI, `gen_endpoint` is the API key
    else:
        raise ValueError(f"Unsupported hosting type: {hosting_type}")

    def process_translation(text):
        return translate_text_with_llm_helper(
            text=text,
            model_id=gen_model,
            client=client,
            hosting_type=hosting_type,
            target_lang=target_lang
        )

    # Parallel translation
    with ThreadPoolExecutor(max_workers=max(1, min(64, len(text_blocks)))) as executor:
        futures = [executor.submit(process_translation, text) for text in text_blocks]
        return [f.result() for f in futures]


def generate_qa_pairs(records, gen_model, gen_endpoint, batch_size=32):

    prompt_template = (
        "You are a helpful assistant creating question-answer pairs for a Retrieval-Augmented Generation (RAG) dataset.\n"
        "Given the following passage, generate **one** question that can be answered strictly using the passage content.\n"
        "Then provide the correct answer from the passage.\n\n"
        "Format your response exactly as:\n"
        "Q: <question>\n"
        "A: <answer>\n\n"

        "Reference Passage:\n{text}\n\n"
        "Q:"
    )

    all_prompts = []
    for r in records:
        prompt = prompt_template.format(text=r.get("page_content"))
        all_prompts.append(prompt)

    qa_pairs = []

    for i in tqdm(range(0, len(all_prompts), batch_size), desc="Generating QA Pairs"):
        batch_prompts = all_prompts[i:i+batch_size]

        payload = {
            "model": gen_model,
            "prompt": batch_prompts,
            "temperature": 0.0,
            "max_tokens": 512
        }

        try:
            response = requests.post(gen_endpoint, json=payload)
            response.raise_for_status()
            result = response.json()
            choices = result.get("choices", [])

            for j, choice in enumerate(choices):
                text = choice.get("text", "").strip()
                if "Q:" in batch_prompts[j]:
                    # Try to split into question and answer
                    parts = text.split("A:", 1)
                    question = parts[0].strip().lstrip("Q:").strip()
                    answer = parts[1].strip() if len(parts) > 1 else ""
                    qa_pairs.append({
                        "question": question,
                        "answer": answer,
                        "context": records[i + j].get("page_content", ""),
                        "chunk_id": records[i + j].get("chunk_id", "")
                    })

        except Exception as e:
            print(f"❌ Error generating QA batch: {e}")

    return qa_pairs
