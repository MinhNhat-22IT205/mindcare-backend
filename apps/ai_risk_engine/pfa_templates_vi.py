"""
Standardized WHO Psychological First Aid (PFA: Look–Listen–Link) & Stanley-Brown Guided Templates
in Vietnamese (`pfa_templates_vi.py`).
"""

from __future__ import annotations

VIETNAM_HOTLINES = [
    {"name": "Cấp cứu Y tế Khẩn cấp", "number": "115", "hours": "24/7 toàn quốc"},
    {"name": "Tổng đài Quốc gia Bảo vệ Trẻ em & Thanh thiếu niên", "number": "111", "hours": "Miễn phí 24/7"},
    {"name": "Đường dây nóng Ngày Mai (Hỗ trợ tâm lý khủng hoảng)", "number": "096 306 1414", "hours": "Hỗ trợ miễn phí"},
    {"name": "Tổng đài Tư vấn Sức khỏe Tâm thần", "number": "1900 1267", "hours": "24/7"},
]

WHO_PFA_L4_LOCKDOWN_RESPONSE = (
    "Mochi đang ở ngay đây với bạn lúc này. Nghe những lời bạn vừa chia sẻ, mình cảm nhận được bạn đang phải chịu đựng "
    "một nỗi đau và sự quá tải rất lớn — sự an toàn của bạn ngay bây giờ là điều quan trọng nhất.\n\n"
    "🌿 **Bước 1 — Dừng lại một nhịp an toàn (Look & Grounding):** Xin hãy tạm rời xa ban công, cầu cao hoặc bất kỳ vật dụng, "
    "thuốc men nào có thể làm đau bạn ngay lúc này. Hãy ngồi xuống một chỗ có ánh sáng và hít chậm một hơi thật nhẹ cùng mình nhé.\n\n"
    "🤍 **Bước 2 — Bạn không phải gánh một mình (Listen):** Cảm giác bế tắc lúc này là thật, nhưng bạn không hề đơn độc và "
    "cơn sóng dữ này có thể dịu xuống khi có người đồng hành trực tiếp.\n\n"
    "📞 **Bước 3 — Kết nối hỗ trợ khẩn cấp ngay bây giờ (Link):**\n"
    "• **Cấp cứu khẩn cấp:** `115`\n"
    "• **Đường dây nóng Ngày Mai:** `096 306 1414`\n"
    "• **Tổng đài Quốc gia hỗ trợ khẩn cấp:** `111` (miễn phí 24/7)\n"
    "• **Tổng đài Sức khỏe Tâm thần:** `1900 1267`\n\n"
    "Bạn hãy bấm ngay nút **Gọi hỗ trợ** hoặc gửi **Tín hiệu SOS ấm áp** cho người thân tin cậy trên màn hình nhé (Mochi cam kết "
    "tuyệt đối không bao giờ gửi nội dung nhật ký riêng tư của bạn)."
)

STANLEY_BROWN_L3_GUIDED_RESPONSE = (
    "Mình nghe thấy bạn đang trải qua những suy nghĩ rất nặng nề và mệt mỏi lúc này. Cảm ơn bạn vì đã không giữ tất cả trong lòng một mình. 🌿\n\n"
    "Mình cùng đi chậm lại từng bước nhỏ theo **Kế hoạch An toàn cá nhân** để giữ bạn an toàn ngay tối nay nhé:\n"
    "1. **Giữ không gian an toàn:** Nếu quanh chỗ bạn ngồi có vật sắc nhọn hoặc thuốc men, bạn giúp Mochi cất chúng sang phòng khác hoặc đưa người nhà giữ giúp nhé.\n"
    "2. **Làm dịu cơ thể 60 giây:** Đặt hai bàn chân chạm sàn, hít vào 4 nhịp – thở ra chậm 6 nhịp để nhịp tim dịu bớt.\n"
    "3. **Kết nối một điểm tựa:** Bạn có muốn bấm gửi một tín hiệu hỗ trợ ấm áp tới người thân tin cậy, hoặc gọi trò chuyện trực tiếp cùng chuyên gia qua **Đường dây nóng Ngày Mai `096 306 1414`**, tổng đài **`111`**, **`1900 1267`** (hoặc **`115`** nếu khẩn cấp) không?\n\n"
    "Bạn thử nói cho Mochi nghe: lúc này bạn đang ở một mình hay có ai ở gần phòng bạn không?"
)

STEP_DOWN_FLOOR_CARE_RESPONSE = (
    "Nghe bạn nói đã thấy dịu hơn phần nào, Mochi mừng vì bạn vẫn đang phản hồi cùng mình. 🌿\n\n"
    "Vì vừa trải qua một cơn căng thẳng rất sâu nên cảm xúc có thể còn chập chờn như sóng ngầm. Mochi vẫn sẽ giữ chế độ "
    "đồng hành an toàn sát cạnh bạn trong hôm nay nhé. Hãy uống một ngụm nước ấm, giữ không gian nghỉ ngơi an toàn, và bất cứ lúc nào "
    "cơn nặng lòng quay lại, bạn luôn có thể gọi **`096 306 1414`**, **`111`**, **`1900 1267`** hoặc **`115`**. Lúc này cơ thể bạn đã đỡ mệt hơn chút nào chưa?"
)

CLARIFICATION_PROBE_L2_RESPONSE = (
    "Nghe bạn nói câu vừa rồi Mochi hơi chùng lòng và lo cho bạn một chút. 🌿 Hôm nay là bài vở, deadline dồn dập làm bạn kiệt sức "
    "nên buột miệng than thở, hay trong lòng bạn đang thật sự xuất hiện suy nghĩ muốn làm đau bản thân vậy?\n\n"
    "Dù là điều gì, bạn cứ nói thật với Mochi nhé — mình ở đây lắng nghe không phán xét chút nào."
)

JAILBREAK_SAFE_REFUSAL_RESPONSE = (
    "Mochi hiểu bạn đang đề cập đến một chủ đề rất nhạy cảm, nhưng mình tuyệt đối không thể cung cấp thông tin về cách thức gây hại "
    "hay xác nhận những suy nghĩ phủ nhận giá trị của bạn — dù dưới góc độ viết truyện hay giả định.\n\n"
    "Cuộc sống và sự an toàn của bạn luôn đáng trân trọng. Nếu bên dưới câu hỏi này là việc chính bạn đang cảm thấy bế tắc hoặc đau khổ, "
    "xin hãy chia sẻ thẳng với Mochi hoặc kết nối ngay với chuyên gia qua **Đường dây nóng Ngày Mai `096 306 1414`**, tổng đài **`111`** "
    "hoặc cấp cứu **`115`** nhé. 🤍"
)

SAFE_REWRITE_FALLBACK_VI = (
    "Mình cảm nhận được bạn đang mang rất nhiều nỗi niềm nặng trĩu lúc này. Cảm xúc đau buồn của bạn hoàn toàn cần được lắng nghe, "
    "nhưng giá trị của bạn không bao giờ mất đi vì những khó khăn hiện tại. Mình luôn ở đây đồng hành để bạn kết nối thêm với những "
    "người thân yêu và chuyên gia hỗ trợ (như Đường dây nóng Ngày Mai 096 306 1414 hoặc tổng đài 111) khi bạn cần nhé. 🌿"
)
