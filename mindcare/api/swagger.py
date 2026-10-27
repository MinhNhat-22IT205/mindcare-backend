"""OpenAPI schema and Swagger UI for the plain-Django MindCare API.

The project deliberately does not depend on Django REST Framework.  The schema
is built from the registered URL patterns and the ``allowed_methods`` metadata
added by ``mindcare.api.common.api`` so new routes cannot silently disappear
from Swagger.
"""

from __future__ import annotations

import json
import re
from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse


API_PREFIX = "/api/v1"

PLACEHOLDER_IDS = {
    "user_id": "11111111-1111-4111-8111-111111111111",
    "checkin_id": "22222222-2222-4222-8222-222222222222",
    "journal_id": "33333333-3333-4333-8333-333333333333",
    "session_id": "44444444-4444-4444-8444-444444444444",
    "run_id": "55555555-5555-4555-8555-555555555555",
    "message_id": "66666666-6666-4666-8666-666666666666",
    "alert_id": "77777777-7777-4777-8777-777777777777",
    "contact_id": "88888888-8888-4888-8888-888888888888",
    "notification_id": "99999999-9999-4999-8999-999999999999",
}

PUBLIC_PATHS = {
    "/auth/register",
    "/auth/login",
    "/auth/password-reset/request",
    "/auth/password-reset/confirm",
    "/trusted-contact-invitations/accept",
    "/trusted-contact-invitations/decline",
}


def _body_example(method: str, path: str) -> dict[str, Any] | None:
    """Return a valid, prefilled JSON body for every write operation."""
    if method not in {"post", "put", "patch"}:
        return None

    examples: dict[tuple[str, str], dict[str, Any]] = {
        ("post", "/auth/register"): {
            "email": "swagger@example.com",
            "password": "Swagger123!",
            "display_name": "Swagger Tester",
            "timezone": "Asia/Ho_Chi_Minh",
        },
        ("post", "/auth/login"): {
            "email": "swagger@example.com",
            "password": "Swagger123!",
        },
        ("post", "/auth/logout"): {},
        ("post", "/auth/password-reset/request"): {"email": "swagger@example.com"},
        ("post", "/auth/password-reset/confirm"): {
            "uid": PLACEHOLDER_IDS["user_id"],
            "token": "token-from-password-reset-email",
            "new_password": "Swagger456!",
        },
        ("patch", "/me"): {
            "display_name": "Swagger Tester",
            "timezone": "Asia/Ho_Chi_Minh",
        },
        ("patch", "/me/preferences"): {
            "reminder_enabled": True,
            "reminder_time": "20:00",
        },
        ("post", "/me/data-deletion"): {
            "password": "Swagger123!",
            "confirmation": "DELETE_MY_ACCOUNT",
        },
        ("post", "/me/consents"): {
            "scope": "ai_checkin",
            "action": "grant",
            "policy_version": "v1.0",
        },
        ("post", "/me/checkins"): {
            "stress_level": 4,
            "mood_level": 7,
            "anxiety_level": 3,
            "energy_level": 6,
            "sleep_hours": 7.5,
            "note": "Hôm nay tôi cảm thấy khá ổn.",
        },
        ("patch", "/me/checkins/{checkin_id}"): {
            "stress_level": 3,
            "mood_level": 8,
        },
        ("post", "/me/journals"): {
            "content": "Hôm nay tôi đã hoàn thành các việc quan trọng và đi bộ 30 phút."
        },
        ("patch", "/me/journals/{journal_id}"): {
            "content": "Tôi đã cập nhật lại ghi chú cho ngày hôm nay."
        },
        ("post", "/me/analysis-runs"): {
            "analysis_type": "deep",
            "period_start": "2025-01-01T00:00:00+07:00",
            "period_end": "2025-01-07T23:59:59+07:00",
        },
        ("post", "/me/chat/sessions"): {"title": "Trao đổi cùng MindCare"},
        ("patch", "/me/chat/sessions/{session_id}"): {
            "title": "Trao đổi đã cập nhật",
            "followup_enabled": True,
        },
        ("post", "/me/chat/sessions/{session_id}/messages"): {
            "message": "Gần đây tôi hơi căng thẳng vì công việc.",
            "allow_followup_question": True,
            "client_message_id": "swagger-message-001",
        },
        ("patch", "/me/chat/messages/{message_id}/feedback"): {
            "helpful": True,
            "recommendation_status": "saved",
        },
        ("post", "/me/chat/messages/{message_id}/confirm-checkin"): {"confirm": False},
        ("put", "/me/safety-plan"): {
            "enabled": True,
            "coping_steps": [
                {"order": 1, "title": "Hít thở chậm trong hai phút"},
                {"order": 2, "title": "Gọi cho một người mình tin tưởng"},
            ],
            "personal_notes": "Tạm rời khỏi nơi gây căng thẳng và uống một cốc nước.",
        },
        ("post", "/me/trusted-contacts"): {
            "display_name": "Nguyễn An",
            "relationship": "friend",
            "contact_type": "email",
            "contact_value": "trusted-contact@example.com",
        },
        ("patch", "/me/trusted-contacts/{contact_id}"): {
            "display_name": "Nguyễn An",
            "relationship": "close_friend",
        },
        ("post", "/me/trusted-contacts/{contact_id}/verification-request"): {},
        ("post", "/trusted-contact-invitations/accept"): {
            "token": "token-from-contact-invitation-email"
        },
        ("post", "/trusted-contact-invitations/decline"): {
            "token": "token-from-contact-invitation-email"
        },
        ("post", "/me/support-requests"): {
            "message": "Tôi muốn được một người hỗ trợ liên hệ.",
            "preferred_support": "human",
        },
        ("post", "/me/alerts/{alert_id}/cancel"): {
            "reason": "Tôi không còn cần gửi yêu cầu này."
        },
        ("post", "/support/alerts/{alert_id}/review"): {
            "decision": "approve",
            "reason": "Đã xem xét và xác nhận yêu cầu.",
        },
        ("patch", "/me/notifications/{notification_id}"): {"is_read": True},
        ("patch", "/admin/users/{user_id}"): {"status": "active", "role": "user"},
        ("post", "/admin/alerts/{alert_id}/assign"): {
            "reviewer_user_id": PLACEHOLDER_IDS["user_id"]
        },
    }
    return examples.get((method, path), {})


def _summary(method: str, path: str) -> str:
    actions = {
        "get": "Get",
        "post": "Create",
        "put": "Replace",
        "patch": "Update",
        "delete": "Delete",
    }
    parts = path.strip("/").split("/")
    resource = parts[-1]
    if resource.startswith("{") and len(parts) > 1:
        resource = parts[-2]
    return f"{actions.get(method, method.title())} {resource.replace('-', ' ').replace('_', ' ')}"


def _tag(path: str) -> str:
    parts = path.strip("/").split("/")
    if parts[0] == "me" and len(parts) > 1:
        return parts[1].replace("-", " ").title()
    return parts[0].replace("-", " ").title()


def _path_parameter(name: str, converter: str) -> dict[str, Any]:
    if converter == "int":
        schema: dict[str, Any] = {"type": "integer", "default": 1, "example": 1}
    else:
        example = PLACEHOLDER_IDS.get(name, f"example-{name}")
        schema = {"type": "string", "default": example, "example": example}
        if converter == "uuid" or name.endswith("_id"):
            schema["format"] = "uuid"
    return {
        "name": name,
        "in": "path",
        "required": True,
        "schema": schema,
        "example": schema["example"],
    }


def _query(name: str, kind: str = "string", default: Any = None, **schema: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"type": kind, **schema}
    if default is not None:
        value["default"] = default
        value["example"] = default
    return {"name": name, "in": "query", "required": False, "schema": value}


def _query_parameters(method: str, path: str) -> list[dict[str, Any]]:
    if method != "get":
        return []

    parameters: list[dict[str, Any]] = []
    paginated = {
        "/me/checkins", "/me/journals", "/me/analysis-runs", "/me/chat/sessions",
        "/me/alerts", "/support/alerts", "/me/notifications", "/admin/users",
        "/admin/analysis-runs", "/admin/alerts",
    }
    if path in paginated:
        parameters.extend([
            _query("page", "integer", 1, minimum=1),
            _query("limit", "integer", 20, minimum=1, maximum=100),
        ])
    if path == "/me/chat/sessions/{session_id}/messages":
        parameters.extend([_query("cursor"), _query("limit", "integer", 30, minimum=1, maximum=100)])

    date_filtered = {
        "/me/checkins", "/me/journals", "/me/dashboard", "/me/trends",
        "/me/emotion-summary", "/me/concern-history", "/me/reports/preview",
    }
    if path in date_filtered:
        parameters.extend([_query("from", "string", None, format="date"),
                           _query("to", "string", None, format="date")])

    extras: dict[str, list[dict[str, Any]]] = {
        "/me/preferences": [],
        "/me/data-export": [
            _query("format", default="json"), _query("include_chat", "boolean", True),
            _query("include_reports", "boolean", True),
        ],
        "/me/journal-prompts": [_query("category", default="stress")],
        "/me/analysis-runs": [_query("type", default="deep"), _query("status")],
        "/me/chat/sessions": [_query("q", default="")],
        "/me/emotion-summary": [_query("source", default="all")],
        "/me/reports/preview": [_query("include", default="stress,mood,sleep")],
        "/me/alerts": [_query("status")],
        "/support/alerts": [_query("status")],
        "/me/notifications": [_query("unread_only", "boolean", False)],
        "/admin/users": [_query("role"), _query("status")],
        "/admin/analysis-runs": [_query("status"), _query("analysis_type")],
        "/admin/alerts": [_query("assigned", "boolean", False), _query("status", default="pending_review")],
    }
    parameters.extend(extras.get(path, []))
    return parameters


def _route_to_openapi(route: str) -> tuple[str, list[dict[str, Any]]]:
    parameters: list[dict[str, Any]] = []

    def replace(match: re.Match[str]) -> str:
        converter = match.group("converter") or "str"
        name = match.group("name")
        parameters.append(_path_parameter(name, converter))
        return "{" + name + "}"

    path = re.sub(
        r"<(?:(?P<converter>[a-zA-Z_][\w]*):)?(?P<name>[a-zA-Z_][\w]*)>",
        replace,
        route,
    )
    return "/" + path.strip("/"), parameters


def _operation(method: str, path: str, path_parameters: list[dict[str, Any]]) -> dict[str, Any]:
    operation_id = re.sub(r"[^a-zA-Z0-9]+", "_", f"{method}_{path}").strip("_")
    operation: dict[str, Any] = {
        "tags": [_tag(path)],
        "summary": _summary(method, path),
        "operationId": operation_id,
        "parameters": [*path_parameters, *_query_parameters(method, path)],
        "responses": {
            "200": {
                "description": "Request successful",
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/SuccessResponse"}}},
            },
            "400": {"$ref": "#/components/responses/BadRequest"},
            "401": {"$ref": "#/components/responses/Unauthorized"},
            "403": {"$ref": "#/components/responses/Forbidden"},
            "404": {"$ref": "#/components/responses/NotFound"},
            "422": {"$ref": "#/components/responses/ValidationError"},
        },
    }
    if path not in PUBLIC_PATHS:
        operation["security"] = [{"BearerAuth": []}]

    example = _body_example(method, path)
    if example is not None:
        operation["requestBody"] = {
            "required": bool(example),
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": True,
                        "default": example,
                        "example": example,
                    },
                    "example": example,
                }
            },
        }
    return operation


def build_openapi_schema(request: HttpRequest | None = None) -> dict[str, Any]:
    """Build the OpenAPI document from the project's versioned URLConf."""
    from mindcare.api.urls import urlpatterns

    paths: dict[str, dict[str, Any]] = {}
    for pattern in urlpatterns:
        methods = getattr(pattern.callback, "allowed_methods", None)
        if not methods:
            continue
        route = getattr(pattern.pattern, "_route", str(pattern.pattern))
        path, path_parameters = _route_to_openapi(route)
        for method in sorted(methods):
            method_lower = method.lower()
            paths.setdefault(path, {})[method_lower] = _operation(
                method_lower, path, path_parameters
            )

    server_url = API_PREFIX
    if request is not None:
        server_url = request.build_absolute_uri(API_PREFIX).rstrip("/")

    error_schema = {"$ref": "#/components/schemas/ErrorResponse"}
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "MindCare API",
            "version": "1.0.0",
            "description": (
                "Swagger cho toàn bộ API MindCare. Body, query và path parameter đều có "
                "giá trị mẫu; nút đăng nhập nhanh sẽ tự gắn Bearer token."
            ),
        },
        "servers": [{"url": server_url}],
        "paths": dict(sorted(paths.items())),
        "components": {
            "securitySchemes": {
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                    "description": "Access token do POST /auth/login trả về.",
                }
            },
            "schemas": {
                "SuccessResponse": {
                    "type": "object",
                    "properties": {
                        "data": {"nullable": True},
                        "meta": {
                            "type": "object",
                            "properties": {"request_id": {"type": "string", "format": "uuid"}},
                        },
                    },
                    "required": ["data", "meta"],
                },
                "ErrorResponse": {
                    "type": "object",
                    "properties": {
                        "error": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string", "example": "VALIDATION_ERROR"},
                                "message": {"type": "string", "example": "Invalid request."},
                                "details": {"type": "array", "items": {}},
                            },
                        },
                        "meta": {
                            "type": "object",
                            "properties": {"request_id": {"type": "string", "format": "uuid"}},
                        },
                    },
                    "required": ["error", "meta"],
                },
            },
            "responses": {
                "BadRequest": {"description": "Invalid request", "content": {"application/json": {"schema": error_schema}}},
                "Unauthorized": {"description": "Authentication required", "content": {"application/json": {"schema": error_schema}}},
                "Forbidden": {"description": "Insufficient permission", "content": {"application/json": {"schema": error_schema}}},
                "NotFound": {"description": "Resource not found", "content": {"application/json": {"schema": error_schema}}},
                "ValidationError": {"description": "Validation failed", "content": {"application/json": {"schema": error_schema}}},
            },
        },
    }


def openapi_schema(request: HttpRequest) -> JsonResponse:
    return JsonResponse(build_openapi_schema(request), json_dumps_params={"ensure_ascii": False})


SWAGGER_HTML = r"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MindCare API - Swagger</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
  <style>
    body { margin: 0; background: #fafafa; }
    .quick-start { position: sticky; top: 0; z-index: 1000; display: flex; gap: 12px;
      align-items: center; padding: 10px 18px; color: #fff; background: #17324d;
      font: 14px/1.4 system-ui, sans-serif; box-shadow: 0 2px 8px #0003; }
    .quick-start button { border: 0; border-radius: 5px; padding: 8px 14px; cursor: pointer;
      font-weight: 650; color: #17324d; background: #8ee3c8; }
    .quick-start button:disabled { cursor: wait; opacity: .65; }
    #quick-status { opacity: .9; }
  </style>
</head>
<body>
  <div class="quick-start">
    <button id="quick-auth" type="button">Tạo/đăng nhập tài khoản test</button>
    <span id="quick-status">Request đã điền sẵn; mở API rồi bấm Execute.</span>
  </div>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-standalone-preset.js"></script>
  <script>
    const API_BASE = %API_BASE%;
    const ids = {};
    const placeholders = %PLACEHOLDERS%;
    let ui;

    function unwrap(payload) {
      return payload && Object.prototype.hasOwnProperty.call(payload, "data") ? payload.data : payload;
    }

    function parseResponse(response) {
      if (response && response.obj) return response.obj;
      const raw = response && (response.text || response.data || response.body);
      if (typeof raw === "object") return raw || {};
      try { return JSON.parse(raw || "{}"); } catch (_) { return {}; }
    }

    function rememberIds(url, method, payload) {
      const data = unwrap(payload) || {};
      const path = new URL(url || window.location.href, window.location.origin).pathname;
      const verb = (method || "").toUpperCase();
      if (verb === "POST" && /\/me\/checkins\/?$/.test(path)) ids.checkin_id = data.id;
      if (verb === "POST" && /\/me\/journals\/?$/.test(path)) ids.journal_id = data.id;
      if (verb === "POST" && /\/me\/chat\/sessions\/?$/.test(path)) ids.session_id = data.id;
      if (verb === "POST" && /\/messages\/?$/.test(path)) {
        ids.user_message_id = data.user_message && data.user_message.id;
        ids.assistant_message_id = data.assistant_message && data.assistant_message.id;
      }
      if (verb === "POST" && /\/analysis-runs\/?$/.test(path)) ids.run_id = data.run_id || data.id;
      if (verb === "POST" && /\/trusted-contacts\/?$/.test(path)) ids.contact_id = data.id;
      if (verb === "POST" && /\/support-requests\/?$/.test(path)) ids.alert_id = data.alert_id;
      const rows = Array.isArray(data) ? data : (data.items || data.results || []);
      if (/\/notifications\/?$/.test(path) && rows[0]) ids.notification_id = rows[0].id;
      if (/\/admin\/users\/?$/.test(path) && rows[0]) ids.user_id = rows[0].id;
    }

    ui = SwaggerUIBundle({
      url: API_BASE + "/openapi.json",
      dom_id: "#swagger-ui",
      deepLinking: true,
      filter: true,
      persistAuthorization: true,
      tryItOutEnabled: true,
      displayRequestDuration: true,
      defaultModelsExpandDepth: -1,
      presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
      layout: "StandaloneLayout",
      requestInterceptor: function(request) {
        Object.entries(placeholders).forEach(function(entry) {
          const key = entry[0], placeholder = entry[1];
          let actual = ids[key];
          if (key === "message_id" && request.url.includes("/feedback")) actual = ids.assistant_message_id;
          if (key === "message_id" && request.url.includes("/confirm-checkin")) actual = ids.user_message_id;
          if (actual) request.url = request.url.replaceAll(placeholder, actual);
        });
        return request;
      },
      responseInterceptor: function(response) {
        const payload = parseResponse(response);
        const data = unwrap(payload) || {};
        const token = data.access_token || data.access || payload.access_token || payload.access;
        if (token && ui) ui.preauthorizeApiKey("BearerAuth", token);
        rememberIds(response.url, response.method, payload);
        return response;
      }
    });

    document.getElementById("quick-auth").addEventListener("click", async function() {
      const button = this;
      const status = document.getElementById("quick-status");
      let email = localStorage.getItem("mindcareSwaggerEmail");
      if (!email) {
        email = "swagger+" + Date.now() + "@example.com";
        localStorage.setItem("mindcareSwaggerEmail", email);
      }
      const credentials = { email: email, password: "Swagger123!" };
      button.disabled = true;
      status.textContent = "Đang tạo và đăng nhập tài khoản test…";
      try {
        await fetch(API_BASE + "/auth/register", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(Object.assign({}, credentials, {
            display_name: "Swagger Tester", timezone: "Asia/Ho_Chi_Minh"
          }))
        });
        const response = await fetch(API_BASE + "/auth/login", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(credentials)
        });
        const payload = await response.json();
        const data = unwrap(payload) || {};
        const token = data.access_token || data.access || payload.access_token || payload.access;
        if (!response.ok || !token) {
          throw new Error((payload.error && payload.error.message) || "Không lấy được access token");
        }
        ui.preauthorizeApiKey("BearerAuth", token);
        status.textContent = "Đã đăng nhập và tự gắn Bearer token. API protected đã sẵn sàng.";
      } catch (error) {
        status.textContent = "Không thể đăng nhập tự động: " + error.message;
      } finally {
        button.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


def swagger_ui(request: HttpRequest) -> HttpResponse:
    html = SWAGGER_HTML.replace("%API_BASE%", json.dumps(API_PREFIX)).replace(
        "%PLACEHOLDERS%", json.dumps(PLACEHOLDER_IDS)
    )
    return HttpResponse(html, content_type="text/html; charset=utf-8")

