# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Toàn bộ workflow là deterministic (không dùng LLM), nằm trong `src/student_agent/workflow.py`.
`cli.py` đọc `inputs/<case_id>.json`, mở một MCP session và gọi `solve_case()` tuần tự cho từng case.

```text
inputs/<case_id>.json
      │  case_received (coordinator)
      ▼
Coordinator ──task_assigned──► Order agent ───── get_order, get_order_items
      ▲                          │ handoff(order_context)
      ├──task_assigned──► Payment agent ─── get_payment_timeline, get_refund_timeline
      │                          │ handoff(payment_context)
      ├──task_assigned──► Shipment agent ── get_shipment_summary
      │                          │ handoff(shipment_context)
      │   classify(order, payment, shipment) → primary_issue
      ├──task_assigned──► Policy agent ──── get_policy → policy_decided
      │                          │ handoff(policy_decision)
      ├──task_assigned──► Order agent ───── get_sellers (chỉ khi seller chịu trách nhiệm)
      │                          │ handoff(seller_lookup)
      └──task_assigned──► Verifier ───────── verification_completed → handoff
      ▼
outputs/<case_id>.json + case_finalized ; mọi event → traces/trace.jsonl
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case input (`case_id`, `claimed_order_id`, `opened_at`, `policy_version`) | Giao task, gom handoff, phân loại `primary_issue`, lắp output | `task_assigned` cho từng specialist; output JSON |
| Order/item | `order_id`, `opened_at` | Lấy order row + item rows, lọc item trong cửa sổ case, xác định item/seller; tra cứu seller khi cần | `handoff(order_context)`, `handoff(seller_lookup)` |
| Payment | `order_id`, cửa sổ case, `order_approved_at` | Lọc payment/refund event trong cửa sổ; tách capture gắn với checkout (±24h quanh approval), reconciliation mismatch, trạng thái refund | `handoff(payment_context)` |
| Shipment | `order_id`, cửa sổ case | Timestamps giao hàng, sự kiện `delivered_late` hợp lệ và actor | `handoff(shipment_context)` |
| Policy | `policy_version`, `primary_issue`, seller đã xác minh | Áp rule policy: `case_status`, action, refund (không vượt số tiền đã capture), responsible parties (seller template → seller thật của order) | `policy_decided`, `handoff(policy_decision)` |
| Verifier | Output dự kiến + tập evidence đã consume | Kiểm tra invariants (mục 6); fail → dừng case | `verification_completed`, `handoff(verification)` |

Quyền gọi tool được khai báo trong `TOOL_OWNERSHIP` và được kiểm tra ở `Agent.fetch()`:

| Actor | Tool được phép |
| --- | --- |
| Order/item | `get_order`, `get_order_items`, `get_sellers` |
| Payment | `get_payment_timeline`, `get_refund_timeline` |
| Shipment | `get_shipment_summary` |
| Policy | `get_policy` |
| Coordinator, Verifier | không gọi tool |

`get_order_payments` (trùng dữ liệu với `get_payment_timeline`), `get_product_context` và `get_customer_history` không cần cho quyết định L3A nên không được gọi.

## 3. A2A protocol

- Envelope: dataclass `Handoff(case_id, sender, recipient, task, findings, evidence)`. Mọi handoff đi về coordinator; specialist không gọi nhau trực tiếp.
- Correlation: mọi message, tool call và trace event đều mang `case_id` của case đang xử lý; agent được khởi tạo riêng cho mỗi case nên không có state chia sẻ giữa các case.
- Điều kiện handoff: payment/shipment chỉ được giao khi order agent xác nhận order tồn tại (`found`). Seller lookup chỉ được giao khi issue có seller chịu trách nhiệm (`unavailable_order_paid`, `late_delivery_seller`).
- Tránh vòng lặp: luồng cố định, mỗi task được giao tối đa một lần/case, không có re-delegation. Timeout HTTP 300s/call; retry có giới hạn (mục 5).
- Trace chỉ ghi event quan sát được: `task_assigned` (target + decision_code = tên task), `tool_result_consumed` (tool + evidence_ref + domain), `handoff` (decision_code kết quả, evidence refs đính kèm), `policy_decided` (issue, status, action, refund), `verification_completed` (VERIFIED hoặc mã lỗi đầu tiên). Không ghi prompt hay lập luận.

## 4. Evidence lifecycle

1. `EvidenceGateway.call()` gửi `case_id` + tham số, validate response theo `mcp-evidence-response-v1.schema.json`.
2. Specialist kiểm tra `order_id` trong data khớp order đang xét, rồi lọc record theo **cửa sổ case** `[order_purchase_timestamp, opened_at]`. Record ngoài cửa sổ (dữ liệu lẫn từ timeline khác) bị loại và ghi vào `data_conflicts`.
3. Mỗi response được dùng sẽ emit `tool_result_consumed` với đúng `evidence_ref` do server trả về (không sửa, không tự tạo).
4. Coordinator chỉ trích dẫn các nhóm evidence hỗ trợ kết luận (`CITED_GROUPS`), ví dụ `late_delivery_logistics` → order + shipment + policy; `refund_failed` → order + payment + refund + policy. Refund timeline chỉ được trích khi có event trong cửa sổ.
5. Verifier xác nhận mọi ref trong output thuộc tập đã consume trong chính case đó → không có evidence chéo case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / lỗi transport | Có: tối đa 3 lần/call (backoff 1.5s, 3s); nếu session chết, CLI mở lại session tối đa 5 lần và tiếp tục từ case chưa hoàn tất | Hết retry → dừng run với lỗi (không sinh output đoán) | Không có event mới cho lần thử lỗi |
| Not found (tool trả `isError`) | Không | Coi như không có record: refund timeline rỗng; thiếu order → `insufficient_evidence`, `needs_investigation`, refund 0 | `handoff` với `ORDER_NOT_FOUND` / `*_EVIDENCE_MISSING` |
| Source conflict | Không | Ưu tiên order row (authoritative) cho status; ưu tiên so sánh timestamp cho actor trễ giao; loại record ngoài cửa sổ case | Ghi vào `data_conflicts`; confidence giảm 0.1 |
| Invalid specialist result | Không | Verifier fail → raise `WorkflowError`, case không được finalize | `verification_completed` với mã lỗi (vd. `REFUND_LINES_TOTAL_MISMATCH`) |

Không bao giờ chuyển evidence thiếu thành dữ liệu phỏng đoán.

## 6. Verification invariants

Trước khi finalize, verifier kiểm tra:

- `case_id` output trùng case đang xử lý (CLI còn validate JSON Schema và `case_id` lần nữa trước khi ghi file).
- Evidence ownership: `evidence_refs` không rỗng và là tập con của ref đã consume trong case.
- Money totals: tổng `refund_lines` = `recommended_refund_brl`; refund policy không vượt số tiền đã capture trong cửa sổ.
- Status/refund/action: `no_action` thì refund = 0; luôn có ít nhất một resolution action.
- Responsibility: mọi responsible party loại `seller` phải có `party_id` nằm trong `affected_entities.seller_ids`.
- Confidence nằm trong [0, 1].

Thứ tự phân loại `primary_issue` (dữ liệu trong cửa sổ case):

1. Order `canceled`/`unavailable` và đã capture tiền → `canceled_order_paid` / `unavailable_order_paid`.
2. Refund `failed` → `refund_failed`; refund `pending` → `refund_pending`.
3. `reconciliation_mismatch` đang `open` → `payment_mismatch`.
4. ≥2 capture ở checkout có tổng = giá + phí ship của item → `valid_split_payment`; ≥2 capture cùng số tiền và tổng vượt giá trị order → `duplicate_charge`.
5. Giao sau ngày dự kiến: carrier nhận hàng sau `shipping_limit_date` → `late_delivery_seller`, ngược lại → `late_delivery_logistics`.
6. Không có bất thường → `unsupported_claim`.

Confidence: 0.95 cơ bản; −0.1 nếu có source conflict; −0.05 nếu có record lẫn nằm trong cửa sổ case; 0.5 cho `insufficient_evidence`.

## 7. Reproducibility

- Không dùng model/LLM, không random: cùng evidence cho ra cùng output.
- Python ≥ 3.11 (đã chạy với 3.13); dependency theo range trong `pyproject.toml` (`mcp>=2,<3`, `httpx2>=2,<3`, `jsonschema>=4.25,<5`).
- Concurrency: 1 (case xử lý tuần tự trên một MCP session). Mỗi case gọi 5–7 tool.
- Lệnh chạy: `day09 validate-inputs && day09 run && day09 validate && day09 package --output dist/submission.zip`.
- Cấu hình qua `.env` (`COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`, `MCP_ENDPOINT`); không commit `.env`, không ghi API key vào output/trace.
