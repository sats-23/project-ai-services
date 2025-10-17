import os

def get_txt_img_tab_filenames(file_paths, out_path):
    original_filenames = [fp.split('/')[-1] for fp in file_paths]
    input_txt_files, input_img_files, input_tab_files = [], [], []
    for fn in original_filenames:
        f, ext = os.path.splitext(fn)
        input_txt_files.append(f'{out_path}/{f}_clean_text.json')
        input_img_files.append(f'{out_path}/{f}_images.json')
        input_tab_files.append(f'{out_path}/{f}_tables.json')
    return original_filenames, input_txt_files, input_img_files, input_tab_files


def get_model_endpoints(deployment_type):
    if deployment_type == 'spyre':
        emb_model_dict = {
            'emb_endpoint': os.getenv("EMB_ENDPOINT"),
            'emb_model':    os.getenv("EMB_MODEL"),
            'max_tokens':   os.getenv("EMB_MAX_TOKENS"),
        }

        llm_model_dict = {
            'llm_endpoint': os.getenv("LLM_ENDPOINT"),
            'llm_model':    os.getenv("LLM_MODEL"),
        }

        reranker_model_dict = {
            'reranker_endpoint': os.getenv("RERANKER_ENDPOINT"),
            'reranker_model':    os.getenv("RERANKER_MODEL"),
        }

        return emb_model_dict, llm_model_dict, reranker_model_dict
    else:
        raise ValueError(f'Endpoints not available for {deployment_type} deployment type.')
