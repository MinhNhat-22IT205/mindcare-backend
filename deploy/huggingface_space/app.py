"""MindCare ESConv Space: load one existing model, generate one JSON response.

Run `manage.py prepare_esconv_space` to bundle this with chat_contract.py.
"""
import json
import logging
import os
from time import perf_counter

import gradio as gr
import spaces
import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, GenerationConfig
from lmformatenforcer import CharacterLevelParserConfig, JsonSchemaParser
from lmformatenforcer.integrations.transformers import (
    build_token_enforcer_tokenizer_data, build_transformers_prefix_allowed_tokens_fn,
)

from chat_contract import (InvalidChatOutput, SpaceInferenceError, parse_result,
                           response_schema, validate_wire_payload)


BASE_MODEL = 'meta-llama/Llama-3.1-8B-Instruct'
ADAPTER_MODEL = 'navidmadani/esconv_sra_llama3_8b'
MAX_INPUT_TOKENS = int(os.getenv('MAX_INPUT_TOKENS', '8192'))
MAX_NEW_TOKENS = int(os.getenv('MAX_NEW_TOKENS', '512'))
MAX_RESPONSE_CHARS = min(4000, int(os.getenv('MAX_RESPONSE_CHARS', '600')))
logger = logging.getLogger('mindcare.esconv')
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
logger.propagate = False
_runtime = None


class DecoderLogFilter(logging.Filter):
    def filter(self, record):
        # LMFE's error handler can include the full token prefix in root logs.
        # Preserve the event, but never persist conversation text or traceback.
        if 'lmformatenforcer' in record.pathname.replace('\\', '/'):
            record.msg = 'Constrained decoder diagnostic; text omitted.'
            record.args = ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


logging.getLogger().addFilter(DecoderLogFilter())


def load_engine():
    token = os.environ.get('HF_TOKEN')
    if not token:
        raise RuntimeError('Set HF_TOKEN with access to the base model in Space Secrets.')
    logger.info('esconv_load phase=tokenizer')
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, token=token)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    quantization = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type='nf4',
                                     bnb_4bit_compute_dtype=torch.bfloat16,
                                     bnb_4bit_use_double_quant=True)
    logger.info('esconv_load phase=base_model')
    base = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL, token=token, quantization_config=quantization,
        device_map='cuda', torch_dtype=torch.bfloat16, low_cpu_mem_usage=True,
    )
    logger.info('esconv_load phase=adapter')
    # ZeroGPU emulates CUDA during startup. PEFT otherwise infers "cuda" and
    # safetensors bypasses that emulation while reading the adapter file.
    # Read the state dict on CPU; PEFT copies it to the base model's layers.
    model = PeftModel.from_pretrained(base, ADAPTER_MODEL, token=token,
                                     is_trainable=False, torch_device='cpu')
    logger.info('esconv_load phase=model_eval')
    model.eval()
    return tokenizer, model


def startup_error_location(exc):
    """Report code locations only, without exception messages, source or locals."""
    frames = []
    tb = exc.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        frames.append(f'{os.path.basename(code.co_filename)}:{tb.tb_lineno}:{code.co_name}')
        tb = tb.tb_next
    return '>'.join(frames[-8:])


def initialize_runtime():
    """Prepare CUDA weights and the vocabulary index before Gradio accepts work.

    ZeroGPU forks GPU workers from this process. Loading inside the decorated
    callback would repeat checkpoint I/O and quantization in each new worker.
    """
    global _runtime
    if _runtime is not None:
        return
    started = perf_counter()
    phase = 'load_model'
    try:
        tokenizer, model = load_engine()
        model_ready = perf_counter()
        phase = 'decoder_setup'
        tokenizer_data = build_token_enforcer_tokenizer_data(tokenizer)
    except Exception as exc:
        code = 'MODEL_LOAD_FAILED' if phase == 'load_model' else 'DECODER_SETUP_FAILED'
        logger.error('esconv_startup_failed code=%s phase=%s error_type=%s location=%s',
                     code, phase, type(exc).__name__, startup_error_location(exc))
        raise RuntimeError(f'ESCONV_{code}') from None
    _runtime = tokenizer, model, tokenizer_data
    ready = perf_counter()
    logger.info('esconv_ready model_load_seconds=%.3f decoder_setup_seconds=%.3f total_seconds=%.3f',
                model_ready - started, ready - model_ready, ready - started)


def generate_once(payload, tokenizer, model, tokenizer_data=None):
    """Exactly one model.generate; malformed output is never regenerated."""
    started = perf_counter()
    messages = validate_wire_payload(payload)
    inputs = tokenizer.apply_chat_template(messages, add_generation_prompt=True,
                                           tokenize=True, return_tensors='pt', return_dict=True)
    if inputs['input_ids'].shape[1] > MAX_INPUT_TOKENS:
        raise SpaceInferenceError('CONTEXT_TOO_LONG')
    if inputs['input_ids'].shape[1] + MAX_NEW_TOKENS > model.config.max_position_embeddings:
        raise SpaceInferenceError('CONTEXT_TOO_LONG')
    inputs = {key: value.to(model.device) for key, value in inputs.items()}
    eos_ids = [tokenizer.eos_token_id]
    eot_id = tokenizer.convert_tokens_to_ids('<|eot_id|>')
    if isinstance(eot_id, int) and eot_id >= 0 and eot_id not in eos_ids:
        eos_ids.append(eot_id)
    try:
        if tokenizer_data is None:
            tokenizer_data = build_token_enforcer_tokenizer_data(tokenizer)
        schema = response_schema()
        schema['properties']['response']['maxLength'] = MAX_RESPONSE_CHARS
        parser_config = CharacterLevelParserConfig(alphabet=tokenizer_data.tokenizer_alphabet,
                                                   force_json_field_order=True,
                                                   max_consecutive_whitespaces=2)
        parser = JsonSchemaParser(schema, config=parser_config)
        prefix_fn = build_transformers_prefix_allowed_tokens_fn(tokenizer_data, parser)
        # LMFE 0.11.3 resets this config while constructing the token enforcer.
        # Restore ordering/whitespace limits without losing the Unicode alphabet.
        parser.config = parser_config
    except Exception:
        raise SpaceInferenceError('DECODER_SETUP_FAILED') from None
    # A fresh deterministic config avoids inherited sampling-only flags.
    generation_config = GenerationConfig(max_new_tokens=MAX_NEW_TOKENS, do_sample=False,
                                         pad_token_id=tokenizer.pad_token_id, eos_token_id=eos_ids,
                                         use_cache=True)
    prepared = perf_counter()
    try:
        with torch.inference_mode():
            output = model.generate(**inputs, generation_config=generation_config,
                                    prefix_allowed_tokens_fn=prefix_fn)
    except Exception as exc:
        code = 'GPU_MEMORY_EXHAUSTED' if type(exc).__name__ == 'OutOfMemoryError' else 'MODEL_GENERATION_FAILED'
        raise SpaceInferenceError(code) from None
    generated = perf_counter()
    generated_ids = output[0][inputs['input_ids'].shape[1]:]
    text = tokenizer.decode(generated_ids, skip_special_tokens=True).strip()
    try:
        result = parse_result(text)
    except InvalidChatOutput:
        code = 'MODEL_OUTPUT_TRUNCATED' if len(generated_ids) >= MAX_NEW_TOKENS else 'INVALID_MODEL_OUTPUT'
        logger.warning('esconv_validation_failed code=%s output_tokens=%d output_chars=%d',
                       code, len(generated_ids), len(text))
        raise SpaceInferenceError(code) from None
    logger.info('esconv_inference input_tokens=%d output_tokens=%d prepare_seconds=%.3f '
                'generate_seconds=%.3f total_seconds=%.3f',
                inputs['input_ids'].shape[1], len(generated_ids), prepared - started,
                generated - prepared, perf_counter() - started)
    return json.dumps(result, ensure_ascii=False)


@spaces.GPU(duration=120)
def generate_chat(payload_json):
    phase = 'payload'
    try:
        if not isinstance(payload_json, str) or len(payload_json.encode()) > 65536:
            raise ValueError('Invalid input payload size.')
        payload = json.loads(payload_json)
        validate_wire_payload(payload)
        phase = 'runtime'
        if _runtime is None:
            raise SpaceInferenceError('MODEL_UNAVAILABLE')
        tokenizer, model, tokenizer_data = _runtime
        phase = 'inference'
        return generate_once(payload, tokenizer, model, tokenizer_data)
    except Exception as exc:
        code = exc.code if isinstance(exc, SpaceInferenceError) else {
            'payload': 'INVALID_PAYLOAD', 'runtime': 'MODEL_UNAVAILABLE',
        }.get(phase, 'MODEL_GENERATION_FAILED')
        logger.error('esconv_failed code=%s phase=%s error_type=%s', code, phase, type(exc).__name__)
        # Emit only this code to Gradio/SSE, never raw exceptions or responses.
        raise gr.Error(f'ESCONV_{code}') from None


with gr.Blocks(title='MindCare ESConv') as demo:
    gr.Markdown('# MindCare ESConv\n3 stages · 7 main strategies + Others · One generation')
    payload = gr.Textbox(label='Backend inference payload (JSON)', lines=8)
    result = gr.Textbox(label='ESConv result (JSON)', lines=8)
    gr.Button('Generate').click(generate_chat, inputs=payload, outputs=result, api_name='generate_chat')


if __name__ == '__main__':
    # Run in the parent process, outside @spaces.GPU, before any worker forks.
    initialize_runtime()
    demo.queue(default_concurrency_limit=1, max_size=32).launch(show_error=False, ssr_mode=False)
