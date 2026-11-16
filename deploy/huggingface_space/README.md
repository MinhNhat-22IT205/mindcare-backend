---
title: MindCare ESConv
sdk: gradio
sdk_version: 5.49.1
python_version: 3.12.12
app_file: app.py
---

MindCare's inference endpoint uses Llama 3.1 8B Instruct with the existing
ESConv-SRA adapter. It requires no further training.

Prepare the upload directory from the backend repository:

```bash
python manage.py prepare_esconv_space --output /tmp/mindcare-esconv-space
```

Upload the prepared files to `VietCristal/mindcare-esconv` when ready to release
the new API. Keep the existing `HF_TOKEN` Space Secret and gated model access.
Use GPU/ZeroGPU hardware. Set `MAX_INPUT_TOKENS=8192` and
`MAX_NEW_TOKENS=512` (the defaults); the backend's input limit must be no larger
than the Space's. No dataset, training job, or extra model is needed.

Generation uses `lm-format-enforcer` to constrain JSON keys and ESConv labels
inside the same model.generate call. `MAX_RESPONSE_CHARS=600` bounds the response
field by default to leave room for JSON within the output budget. Both this
schema bound and the model's token limit apply; an incomplete output is rejected.

The model is placed on CUDA and the decoder vocabulary index is built once in
the parent process, before Gradio starts accepting requests. Do not move these
loads into `@spaces.GPU`: ZeroGPU workers can be recreated between requests,
which would repeat checkpoint loading even with a worker-local Python cache.
This follows [ZeroGPU's model-loading guidance](https://huggingface.co/docs/hub/en/spaces-zerogpu#model-loading).
Startup takes longer; ordinary requests reuse the prepared state.

PEFT reads adapter tensors with `torch_device="cpu"` during startup, then copies
them into the base model's layers. Without this explicit reader device, PEFT
can select CUDA because ZeroGPU reports it as available, while safetensors calls
the real CUDA allocator before a GPU exists. This causes startup to fail after
`adapter_model.safetensors` downloads. See [PEFT issue #3793](https://github.com/huggingface/peft/issues/3793).
Keep this CPU read when changing the model loader; the model still runs on GPU.

API name: `/generate_chat`. Input: one string `payload_json` containing
`{"messages": [...], "prompt_version": "esconv-system-v1"}`. Messages are
system, completed user/assistant pairs, then the current user message.
Output: one JSON string containing exactly `stage`, `strategy`, `response`.
Definitions and validation are bundled in `chat_contract.py` by the command.
The new endpoint replaces the POC's manual strategy selection.

The backend submits once to `/gradio_api/call/generate_chat` and reads the
corresponding event stream. Polling does not trigger another generation.
Invalid JSON, token overflow or inference failure produce a generic Gradio
error; the backend uses its local fallback. Neither side retries generation.
Do not use real personal conversations for deployment smoke checks.

On failure, Space logs include `esconv_failed code=... phase=... error_type=...`.
Codes distinguish `MODEL_LOAD_FAILED`, `DECODER_SETUP_FAILED`,
`GPU_MEMORY_EXHAUSTED`, `MODEL_GENERATION_FAILED`, `INVALID_MODEL_OUTPUT`, and
`MODEL_OUTPUT_TRUNCATED`. The backend stores known codes in its internal
`fallback_reason`. Logs never include prompts, response text or raw exception
messages. Gradio SSR is disabled to keep API/queue traffic on the Python server.

Startup logs mark `esconv_load phase=tokenizer|base_model|adapter|model_eval`.
If startup fails, `esconv_startup_failed` includes the exception type and up to
8 code locations in `location` (filename, line number, function). No exception
message, source line or local variables are logged. The last `esconv_load` phase
and these locations identify the failing loader without exposing credentials.

For latency checks, wait for `esconv_ready` after startup, then send 3 short
synthetic conversations. `esconv_ready` reports model/decoder startup times;
each successful `esconv_inference` reports input/output token counts,
`prepare_seconds`, `generate_seconds`, and `total_seconds`. These are wall times
inside the GPU callback, excluding the Gradio queue, GPU allocation and network
transport. Compare them with the caller's elapsed time before adjusting budgets.
Checkpoint loading should occur during startup, not between these requests.
The fix removes repeated initialization; it cannot guarantee a fixed ZeroGPU
response time. Keep the output budget large enough to close the constrained JSON.
