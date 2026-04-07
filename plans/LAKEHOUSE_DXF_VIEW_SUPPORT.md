# Lakehouse Server — Add DXF/DWG Rendering to /view

The bridge currently renders DXF files locally using ezdxf + matplotlib.
This should move to the lakehouse service's `/view` endpoint so the bridge
stays a thin MCP wrapper with no format-specific rendering.

---

## What to do

Add DXF/DWG support to `GET /api/v1/assets/{asset_id}/view`.

When content_type is `application/dxf`, `application/acad`, or the filename
ends in `.dxf` / `.dwg`:

1. Download the file bytes from S3
2. If DWG: convert to DXF via `dwg2dxf` (libredwg)
3. Render DXF to PNG using ezdxf + matplotlib (same logic currently in
   the bridge's `_render_dxf_drawing`)
4. Return the same response shape as PDFs:

```json
{
  "asset_id": "...",
  "filename": "drawing.dxf",
  "content_type": "application/dxf",
  "format": "pages",
  "total_pages": 1,
  "pages": [
    {"page_num": 1, "base64": "<png base64>", "width": ..., "height": ...}
  ]
}
```

Dependencies to add: `ezdxf`, `matplotlib`. Optional: `libredwg` for DWG.

---

## Bridge cleanup (after deploy)

Once this is live, the bridge team will:
- Remove `_render_dxf_drawing` from `tools/lakehouse_tools.py`
- Remove the DXF special-case branches in `handle_view_asset_with_vision`
  and `handle_view_asset_text` — DXF flows through `get_asset_view()` like
  everything else

---

*Created: 2026-04-07*
