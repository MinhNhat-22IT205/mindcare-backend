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
    "risk_level": "...",
    "summary": "...",
    "recommendations": [],
    "contributing_factors": [],  # tùy chọn
    "dangerous_signals": [],    # tùy chọn
}
```

Safety screening cần adapter riêng qua `MINDCARE_SAFETY_DETECTOR`. Adapter nhận nội dung một tin nhắn và trả về `None`, hoặc dictionary gồm `severity` và `reason`. Service gọi detector trên từng tin nhắn, không đợi gom batch. Khi chưa cấu hình adapter, workflow tương ứng báo lỗi cấu hình thay vì dùng bộ từ khóa hoặc kết quả phân tích giả định.
