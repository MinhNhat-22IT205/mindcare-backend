# Hướng dẫn phát triển

## Yêu cầu

- Python 3.12 trở lên
- `venv` (được tích hợp sẵn trong Python)

## Tạo môi trường và cài dependencies

Tại thư mục gốc của repository, tạo virtual environment:

```bash
python3 -m venv .venv
```

Kích hoạt môi trường:

- Linux/macOS:
  ```bash
  source .venv/bin/activate
  ```
- Windows PowerShell:
  ```powershell
  .venv\Scripts\Activate.ps1
  ```

Cài dependencies:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Chạy ứng dụng

Chạy database migrations:

```bash
python manage.py migrate
```

Khởi động Django development server:

```bash
python manage.py runserver
```

Mở http://127.0.0.1:8000 trong trình duyệt. Dừng server bằng `Ctrl+C` và thoát virtual environment bằng `deactivate`.

## Biến môi trường tùy chọn

- `DJANGO_SECRET_KEY`: secret key cho Django. Khi không đặt, ứng dụng dùng giá trị mặc định dành cho phát triển.
- `DJANGO_ALLOWED_HOSTS`: danh sách hostname phân tách bằng dấu phẩy; mặc định là `localhost,127.0.0.1`.

## Kết nối Neon PostgreSQL

Tạo file `.env` ở thư mục gốc dự án (file này đã được Git bỏ qua) và đặt connection string lấy từ Neon:

```env
DATABASE_URL="postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require"
```

Thay các giá trị mẫu bằng connection string của Neon. Nếu mật khẩu có ký tự đặc biệt, dùng URL-encoded connection string do Neon cung cấp. Cài lại dependencies nếu chưa cài `psycopg` và `python-dotenv`:

```bash
pip install -r requirements.txt
```

Sau đó chạy migration như bình thường:

```bash
python manage.py migrate
```

Khi chưa đặt `DATABASE_URL`, ứng dụng tiếp tục dùng SQLite cục bộ.

## Cấu hình service phân tích

Policy Light/Deep có thể điều chỉnh bằng biến môi trường:

- `MINDCARE_LIGHT_WINDOW_DAYS`, `MINDCARE_COMPARISON_WINDOW_DAYS`
- `MINDCARE_CHANGE_THRESHOLD`, `MINDCARE_CHANGE_METRICS`
- `MINDCARE_LIGHT_MINIMUM_CHECKINS`, `MINDCARE_DEEP_MINIMUM_CHECKINS`
- `MINDCARE_CHAT_BATCH_SIZE`, `MINDCARE_CHAT_BATCH_WINDOW_HOURS`
- `MINDCARE_DEEP_WINDOW_DAYS`, `MINDCARE_WEEKLY_BUCKET_LIMIT`

Các giá trị mặc định nằm trong `config/settings.py`; có thể ghi đè theo môi trường mà không sửa service. `MINDCARE_CHANGE_METRICS` nhận tên các trường của `CheckIn`, phân tách bằng dấu phẩy.

Deep Analysis cần adapter LLM được cấu hình qua `MINDCARE_DEEP_ANALYZER` theo dotted path Python, ví dụ `myapp.analysis.deep_analyzer`. Adapter nhận context dạng dictionary và trả về tối thiểu:

```python
{
    "emotion_summary": {},
    "concern_level": "...",
    "summary": "...",
    "recommendations": [],
    "contributing_factors": [],  # tùy chọn
    "dangerous_signals": [],    # tùy chọn
}
```

Safety screening mặc định dùng `mindcare.services.chat_safety.detect_safety`, là policy chạy cục bộ trên từng tin nhắn. Output guard mặc định cũng chạy cục bộ. Các quy tắc nhận diện tín hiệu rõ ràng, không đưa ra đánh giá an toàn lâm sàng khi không có match. Có thể thay bằng policy nội bộ qua dotted path; detector/guard cho luồng ESConv phải chạy cục bộ, không gọi thêm LLM. Thiếu policy hoặc policy lỗi thì API trả `503 SAFETY_DETECTOR_UNAVAILABLE` và không lưu lượt chat dở dang.

## AI Chat ESConv — một model, một lượt suy luận

Chat dùng model trên `VietCristal/mindcare-esconv` với prompt ESConv: model tự chọn stage, chọn strategy rồi sinh phản hồi trong cùng một lần `generate`. Backend xử lý safety/context/output validation bằng code, không gọi thêm model để phân loại, tóm tắt history, sửa JSON hoặc sinh lại. Adapter dùng giao thức Gradio submit + SSE bằng thư viện chuẩn Python, không cần thêm SDK vào backend.

### Chuẩn bị và cập nhật Space

Nếu Space vẫn dùng API POC `/generate(message, strategy)`, endpoint đó chưa nhận history và chưa trả stage/strategy. Repository có mã Space tại `deploy/huggingface_space/`; chuẩn bị thư mục phát hành bằng:

```bash
python manage.py prepare_esconv_space --output /tmp/mindcare-esconv-space
```

Lệnh tạo 4 file (`app.py`, `chat_contract.py`, `requirements.txt`, `README.md`) trong thư mục trống; không upload, không thay Space đang chạy và không sao chép secrets. Sau khi kiểm tra, đưa 4 file này lên Space `VietCristal/mindcare-esconv` để triển khai endpoint `/generate_chat`. Giữ `HF_TOKEN` trong Space Secrets và quyền truy cập Llama hiện có. Space dùng một base model với adapter ESConv đã có, không huấn luyện thêm.

Space mặc định dùng `MAX_INPUT_TOKENS=8192`, `MAX_NEW_TOKENS=512`. Backend dùng giới hạn input tương ứng, ước lượng bảo thủ bằng số byte UTF-8 và phần dự phòng cho chat template; Space kiểm tra lại số token thực tế trước inference. Cách này có thể loại bớt history sớm hơn tokenizer thực tế nhưng không tự cắt tin nhắn hiện tại. System prompt + message vượt ngân sách → `422 CONTEXT_TOO_LONG`.

Space ràng buộc JSON ngay khi model chọn token bằng `lm-format-enforcer`; stage/strategy vẫn do model chọn trong cùng lần `generate`. Mặc định `MAX_RESPONSE_CHARS=600` trên Space để dành token cho các field JSON và phần kết thúc; API vẫn chấp nhận text tối đa 4000 ký tự. Sau khi cập nhật bản vá, upload đủ cả 4 file, gồm `requirements.txt` và `chat_contract.py`, để dependency/schema đồng bộ.

Model được đặt trên CUDA và bộ chỉ mục tokenizer cho JSON được nạp trong process chính, trước khi mở Gradio queue. Không nạp chúng bên trong `@spaces.GPU`: ZeroGPU có thể tạo worker mới cho mỗi lượt, khiến cache Python trong worker không tránh được việc nạp checkpoint lại. Cách chuẩn bị lúc khởi động tuân theo [hướng dẫn ZeroGPU](https://huggingface.co/docs/hub/en/spaces-zerogpu#model-loading). Space mất thời gian nạp lúc khởi động; chờ log `esconv_ready` trước khi thử API.

Khi nạp adapter PEFT ở startup, phải truyền `torch_device="cpu"` để đọc file trọng số qua CPU rồi chép vào các layer của base model. ZeroGPU báo CUDA khả dụng trong chế độ mô phỏng; nếu bỏ tham số này, PEFT/safetensors có thể gọi CUDA thật trước khi được cấp GPU và dừng sau khi tải `adapter_model.safetensors` ([PEFT #3793](https://github.com/huggingface/peft/issues/3793)). Model vẫn suy luận trên GPU; không đưa việc nạp lại vào mỗi request để tránh lỗi này.

### Cấu hình backend

Trong `.env` (đã được Git bỏ qua):

```env
MINDCARE_DATA_ENCRYPTION_KEY=<Fernet key đang dùng của dự án>
MINDCARE_ESCONV_SPACE_URL=https://vietcristal-mindcare-esconv.hf.space
MINDCARE_ESCONV_API_NAME=generate_chat
MINDCARE_ESCONV_TIMEOUT_SECONDS=120
MINDCARE_CHAT_MAX_INPUT_TOKENS=8192
MINDCARE_CHAT_MAX_OUTPUT_CHARS=4000
```

Space private có thể đặt `MINDCARE_ESCONV_HF_TOKEN` ở backend để xác thực transport; key dùng tải model vẫn ở Space Secrets. Không thay encryption key nếu đã có dữ liệu mã hóa. Xóa các override Groq cũ cho `MINDCARE_SAFETY_DETECTOR`, `MINDCARE_CHAT_RESPONDER`, `MINDCARE_CHAT_OUTPUT_GUARD`, hoặc đặt rõ các giá trị mới:

```env
MINDCARE_SAFETY_DETECTOR=mindcare.services.chat_safety.detect_safety
MINDCARE_CHAT_RESPONDER=mindcare.integrations.esconv.respond
MINDCARE_CHAT_OUTPUT_GUARD=mindcare.services.chat_safety.guard_output
```

`GROQ_API_KEY` không tự kích hoạt luồng Groq nữa. Module Groq cũ được giữ để tham khảo; output `content/intent/action` không đáp ứng hợp đồng ESConv. Khởi động lại Django sau khi đổi `.env`. Nếu Space chưa cập nhật hoặc không sẵn sàng, API lưu phản hồi dự phòng với `status=degraded`; không tự chuyển sang Groq.

### Thử API

Tại Swagger `http://127.0.0.1:8000/api/v1/docs/`:

1. Đăng nhập và gắn JWT Bearer.
2. Tạo session bằng `POST /api/v1/me/chat/sessions` với `{"title":"Thử ESConv"}`.
3. Gửi `POST /api/v1/me/chat/sessions/{session_id}/messages` với `{"message":"Hôm nay mình thấy áp lực vì đồ án.","client_message_id":"demo-001","allow_followup_question":true}`.
4. Response `201` có `assistant_message.content` và metadata `stage`, `strategy`, `status`. `ok` là output hợp lệ; `degraded` là template fallback; `safety_override` là nhánh safety không gọi model. Hai trạng thái fallback có stage/strategy=null. Model/prompt/source/error chi tiết được giữ nội bộ.
5. Gửi tin tiếp theo với ID mới để kiểm tra history; gửi lại cùng ID và payload để nhận đúng kết quả cũ mà không inference lại. Cùng ID nhưng payload khác → `409 DUPLICATE_MESSAGE_CONFLICT`; lượt đang xử lý → `409 MESSAGE_IN_PROGRESS`.
6. Tắt `followup_enabled` của session hoặc gửi `allow_followup_question=false` để ngăn câu hỏi bổ sung. Output vi phạm tùy chọn bị thay bằng fallback, không sinh lại.

History luôn lấy các cặp user/assistant hoàn tất của **chính session**, kể cả khi không có `chat_context`. Bổ sung journal/check-in/report cần `chat_context` và consent tương ứng. Report outdated, nguồn đã sửa/xóa/thu hồi consent bị loại; quyền và version nguồn được kiểm tra lại trước và sau inference. Thiếu dữ liệu không chặn hỗ trợ cảm xúc. Chat ESConv không cần RAG index.

Khi gặp `degraded`, xem dòng `esconv_failed code=... phase=... error_type=...` trong Space Logs và `ESConv fallback reason=...` ở terminal backend. `MODEL_LOAD_FAILED` là lỗi nạp model/adapter; `DECODER_SETUP_FAILED` là lỗi chuẩn bị ràng buộc JSON; `GPU_MEMORY_EXHAUSTED` là thiếu bộ nhớ GPU; `MODEL_GENERATION_FAILED` là lỗi generate; `INVALID_MODEL_OUTPUT` là output sai schema; `MODEL_OUTPUT_TRUNCATED` là output chưa hợp lệ khi đã chạm giới hạn token. Log chỉ ghi mã lỗi và số lượng token/ký tự, không ghi nội dung hội thoại. Bản Space cũ chỉ báo “ESConv inference unavailable or output invalid”, nên không đủ bằng chứng phân biệt nguyên nhân từ dòng đó.

Nếu Space dừng trước `esconv_ready`, xem phase cuối trong `esconv_load` (`tokenizer`, `base_model`, `adapter`, `model_eval`) và dòng `esconv_startup_failed`. Field `location` chứa tối đa 8 vị trí mã nguồn (tên file, số dòng, tên hàm), không chứa raw exception, dòng source hay biến cục bộ. Cảnh báo pip chạy bằng root và `torch_dtype` deprecated không tự chứng minh nguyên nhân của `MODEL_LOAD_FAILED`.

Nếu mỗi lượt mất 36–46 giây, thử 3 lượt ngắn bằng dữ liệu giả sau `esconv_ready`. Log `esconv_inference` có `input_tokens`, `output_tokens`, `prepare_seconds`, `generate_seconds`, `total_seconds`; không chứa nội dung. Đây là thời gian bên trong GPU callback, chưa gồm Gradio queue, cấp GPU và mạng. Nếu UI/API chậm hơn nhiều so với `total_seconds`, kiểm tra thời gian chờ bên ngoài callback. Nếu `generate_seconds` chiếm phần lớn, so sánh số token input/output trước khi đổi giới hạn. Checkpoint chỉ được nạp lúc khởi động, không nạp lại giữa các lượt. Bản sửa loại bỏ việc khởi tạo lặp lại, không bảo đảm một độ trễ cố định trên ZeroGPU; giảm mạnh `MAX_NEW_TOKENS` có thể làm JSON bị cắt.

Sau khi sửa lỗi, thử với session mới hoặc `client_message_id` mới. Gửi lại ID của một lượt đã lưu fallback sẽ nhận lại đúng fallback cũ theo cơ chế idempotency.

Backend commit user message trước inference, khóa session khi nhận lượt và ghi kết quả. Hai request không được inference đồng thời cho cùng session. Với PostgreSQL, cơ chế dùng row lock; SQLite xử lý xung đột ghi bằng `MESSAGE_IN_PROGRESS`. Nếu process dừng sau khi đã gửi inference nhưng trước khi lưu kết quả, lượt chưa có assistant tiếp tục trả `MESSAGE_IN_PROGRESS`; không tự phát lại inference vì không biết model đã thực hiện hay chưa. Cần đối soát lượt dở dang trước khi mở lại session. Khi triển khai nhiều process, cấu hình cache dùng chung cho rate limit.

### Light Analysis sau chat

`MINDCARE_CHAT_ANALYSIS_ENQUEUE(message_id)` là adapter enqueue riêng, chỉ được gọi **sau commit** khi `ai_chat` còn hiệu lực. Worker gọi `mindcare.services.chat.process_chat_light_analysis(message_id)`, kiểm tra lại consent rồi chạy Light Analysis có sẵn. Callback trả `False` hoặc ném exception khi không enqueue được. Chưa cấu hình callback → `analysis_queued=false`; không tạo job giả hoặc chặn câu trả lời chat. Light/Deep Analysis là nghiệp vụ riêng, không tăng số lượt suy luận của pipeline chat.

## API `/api/v1`

Swagger UI có tại http://127.0.0.1:8000/api/v1/docs/ và OpenAPI JSON có tại
http://127.0.0.1:8000/api/v1/openapi.json. Swagger tự mở chế độ Try it out và
điền sẵn body/query/path parameter. Bấm **Tạo/đăng nhập tài khoản test** để tạo
tài khoản thử nghiệm, đăng nhập và tự gắn Bearer token cho các API cần xác thực.
Các ID trả về khi tạo check-in, journal, chat session, analysis run, contact hoặc
alert cũng được Swagger ghi nhớ để thay cho ID mẫu ở các request tiếp theo.

Các route trong `API_Doc.md` đã được nối vào Django. Dữ liệu journal, chat, contact, safety plan và lý do yêu cầu hỗ trợ dùng Fernet; trước khi ghi/đọc các dữ liệu đó, đặt `MINDCARE_DATA_ENCRYPTION_KEY` thành một khóa Fernet riêng, lưu ngoài database. Có thể tạo khóa bằng:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Giữ khóa để giải mã dữ liệu cũ; không thay khóa trực tiếp khi đã có dữ liệu. Nếu database đang chứa plaintext từ POC, chạy lệnh `python manage.py encrypt_legacy_data` sau khi đã cấu hình khóa và sao lưu dữ liệu.

Các tích hợp chưa có sẵn trong repository cần cấu hình adapter theo dotted path:

- `MINDCARE_ANALYSIS_ENQUEUE(run_id)`: đưa run đã lưu vào worker; worker gọi `DeepAnalysisService.run_for_queued(run_id)`. Chưa cấu hình thì POST analysis trả 503 và không tạo job treo.
- `MINDCARE_SAFETY_DETECTOR(text)`: policy cục bộ trả `None` hoặc `{severity, reason}` với severity `urgent|needs_review`. Thiếu detector thì gửi chat trả 503.
- `MINDCARE_CHAT_RESPONDER(payload)`: nhận `{messages, prompt_version}`, trả `{stage, strategy, response}` đúng schema trong `mindcare/chat_contract.py`. `MINDCARE_CHAT_OUTPUT_GUARD(text)` chạy cục bộ và phải trả `True`; lỗi dùng template `status=degraded` với nhãn null, không inference lại. Proposal check-in chỉ trích bằng quy tắc từ dữ kiện định lượng rõ ràng, không thêm model call.
- `MINDCARE_ALERT_DELIVERY_ENQUEUE(alert_id)`: đưa delivery vào worker. Worker gọi `deliver_queued_alert(alert_id)` với `MINDCARE_ALERT_SENDER(channel=..., recipient=..., message=...)` đã cấu hình. Worker kiểm tra lại consent và trạng thái contact ngay trước lúc gửi.
- `MINDCARE_PASSWORD_RESET_URL` và `MINDCARE_CONTACT_INVITATION_URL`: URL frontend dùng trong email token; cấu hình SMTP Django và `DEFAULT_FROM_EMAIL` cho luồng email.
- `MINDCARE_RAG_MANIFEST`: đường dẫn manifest JSON đã duyệt cho endpoint trạng thái index; có thể đặt `MINDCARE_RAG_EMBEDDING_MODEL` để từ chối manifest không tương thích.

Rate limit mặc định dùng Django cache. Khi chạy nhiều worker, cấu hình cache dùng chung như Redis và đặt `DJANGO_SECRET_KEY` riêng, không dùng khóa mặc định dành cho môi trường phát triển.
`client_message_id` được chống trùng trong phạm vi một session bằng transaction và khóa session; để bảo đảm exactly-once trên nhiều worker cần thêm ràng buộc/lưu idempotency bền vững. Các adapter và provider nêu trên là điều kiện để chạy đầy đủ luồng AI/RAG/email/delivery; khi chưa cấu hình, endpoint trả trạng thái lỗi hoặc fallback như mô tả, không tạo kết quả AI giả.
