# ChromaScript interaction contract
Product evidence: the accepted ChromaScript plan, shared Python models, and the operation registry.
Locale: zh-CN. Dates use Asia/Taipei; units and code identifiers remain explicit.

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
|---|---|---|---|---|
| Form | frontend/src/components.tsx Field / Textarea | shared schema + this contract | project, render, delivery configuration, code | browser failure paths |
| Select/Listbox | native select via Field | DESIGN.md | OS popup | keyboard and open popup |
| Scrollbar | frontend/src/style.css | DESIGN.md generated tokens | root and panels | computed style |
| Toast | frontend/src/components.tsx Notice | this contract | status, persistent error | live region |
| CRUD | shared API + frontend/src/api.ts | operation registry | create, revision edit, archive/restore | browser + MCP |
| Modal | frontend/src/components.tsx Modal | this contract | create, discard draft, sequence export | focus, Escape |

Mutations are pessimistic; keep values until confirmed and disable duplicate submits. Saving code sends expected_revision_id. Conflicts preserve the local draft and offer reload/copy. Scene/project changes guard unsaved drafts; browser beforeunload is used only when dirty.
Create keeps the new item selected. Cancel returns to the same view. Archive is reversible and the server rejects active jobs. Cancelling a job preserves cached frames; resumption is an explicit action.
No automatic recurring rendering. Polling reads state and never submits jobs. Late project/scene/image responses must not replace a newer selection.
Loading reserves media geometry. Errors remain visible with correction/retry actions. A disconnected server preserves drafts and provides reconnect. Searches are local, clear immediately, use pagination and never submit during IME composition.
Keyboard operation uses native semantics. Dialogs trap focus natively, support Escape, and restore focus. Pixel sampling has a keyboard center-sample alternative. No drag-only functionality.
MCP tools and CLI share identical operations. Every product capability has an operation entry and generated schema. There is no permanent-delete UI or external publication in v1.

Sequence export copies all frames and the sidecar through export_artifact. The destination stays editable after validation or filesystem errors; existing data is not replaced. Individual EXR outputs and manifests are downloadable, and successful exports expose a copyable absolute path.

Delivery uses directly selected container, codec, profile, depth, sampling, color, alpha, range, compression and quality fields. The current user request replaces preset-based selection. The shared Python delivery capability matrix is authoritative; all transports use get_delivery_options and submit_configured_delivery. Each target is a transient draft until a job freezes its complete output description. Legacy preset APIs remain only for script/history compatibility.
Linked selections prioritize the changed field, retain compatible values and announce automatic adjustments. Alpha can select a compatible 4444 profile; HDR can select a compatible depth; MXF constrains shared frame-rate choices. User-supplied invalid submission values are rejected rather than silently rewritten at submission. Stale capability responses never replace newer drafts. Pending or invalid targets block delivery, errors retain fields and offer retry, and adding/removing an output never changes other outputs. Delivery drafts are transient and intentionally stay out of URL query parameters.
