"""Read-only web dashboard.

A third adapter alongside ``rest.py`` and ``voice.py``, and thin for the same
reason: it renders whatever ``patient_service`` returns and owns no logic of
its own. The page is served as a single self-contained document - no build
step, no CDN, no framework - because a dashboard that needs its own toolchain
is a liability in a project this size.

Read-only by design. Creating or deleting patients is the REST API's job, and
an unauthenticated write surface on the public internet is not something to
add for a demo.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.infra.db import get_session
from app.services import patient_service

router = APIRouter(tags=["dashboard"])

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CloudCare Clinic - Registered Patients</title>
<style>
  :root {{
    --bg: #f6f7f9; --card: #fff; --line: #e3e6ea; --text: #1a1d21;
    --muted: #6b7280; --accent: #1f6feb;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 2rem 1.25rem; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  .wrap {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
  .sub {{ color: var(--muted); margin: 0 0 1.5rem; font-size: .9rem; }}
  form {{ display: flex; gap: .5rem; flex-wrap: wrap; margin-bottom: 1.25rem; }}
  input {{
    padding: .5rem .7rem; border: 1px solid var(--line); border-radius: 6px;
    font-size: .9rem; min-width: 170px; background: var(--card);
  }}
  button {{
    padding: .5rem 1rem; border: 0; border-radius: 6px; background: var(--accent);
    color: #fff; font-size: .9rem; cursor: pointer;
  }}
  a.clear {{ align-self: center; color: var(--muted); font-size: .85rem; }}
  .card {{
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    overflow-x: auto;
  }}
  table {{ width: 100%; border-collapse: collapse; font-size: .88rem; }}
  th, td {{ padding: .7rem .9rem; text-align: left; white-space: nowrap; }}
  th {{
    background: #fafbfc; border-bottom: 1px solid var(--line); font-weight: 600;
    color: var(--muted); text-transform: uppercase; font-size: .72rem;
    letter-spacing: .04em;
  }}
  tr + tr td {{ border-top: 1px solid var(--line); }}
  td.muted {{ color: var(--muted); }}
  .empty {{ padding: 2.5rem; text-align: center; color: var(--muted); }}
  .count {{ color: var(--muted); font-size: .85rem; margin: 1rem 0 0; }}
  code {{ background: #eef1f4; padding: .1rem .35rem; border-radius: 4px; font-size: .85em; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Registered Patients</h1>
  <p class="sub">CloudCare Clinic &middot; live view of the patients table &middot;
     soft-deleted records are excluded</p>

  <form method="get" action="/">
    <input name="last_name" placeholder="Last name" value="{last_name}">
    <input name="phone_number" placeholder="Phone number" value="{phone_number}">
    <input name="date_of_birth" placeholder="DOB (MM/DD/YYYY)" value="{date_of_birth}">
    <button type="submit">Search</button>
    <a class="clear" href="/">Clear</a>
  </form>

  <div class="card">{table}</div>
  <p class="count">{count} &middot; data from
     <code>GET /patients</code> &middot; <a href="/docs">API docs</a></p>
</div>
</body>
</html>"""


def _esc(value: object) -> str:
    """Escape untrusted text before it reaches the page.

    Patient values arrive from callers via speech, so they are untrusted input
    even though the validators constrain most fields.
    """
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _spoken_phone(raw: str | None) -> str:
    """+15551234567 -> (555) 123-4567, which is how a person reads it."""
    if not raw:
        return ""
    digits = "".join(c for c in raw if c.isdigit())[-10:]
    if len(digits) != 10:
        return _esc(raw)
    return "({}) {}-{}".format(digits[:3], digits[3:6], digits[6:])


_COLUMNS = ("Name", "DOB", "Sex", "Phone", "Email", "Address",
            "Insurance", "Emergency contact", "Language", "Registered")


def _row(p: dict) -> str:
    address = ", ".join(
        part for part in (
            p.get("address_line_1"), p.get("address_line_2"),
            "{} {} {}".format(
                p.get("city") or "", p.get("state") or "", p.get("zip_code") or ""
            ).strip(),
        ) if part
    )
    insurance = " ".join(
        part for part in (p.get("insurance_provider"), p.get("insurance_member_id"))
        if part
    )
    emergency = " ".join(
        part for part in (
            p.get("emergency_contact_name"),
            _spoken_phone(p.get("emergency_contact_phone")),
        ) if part
    )
    cells = [
        _esc("{} {}".format(p.get("first_name") or "", p.get("last_name") or "").strip()),
        _esc(p.get("date_of_birth")),
        _esc(p.get("sex")),
        _spoken_phone(p.get("phone_number")),
        _esc(p.get("email")),
        _esc(address),
        _esc(insurance),
        emergency,
        _esc(p.get("preferred_language")),
        _esc((p.get("created_at") or "")[:10]),
    ]
    return "<tr>" + "".join(
        '<td{}>{}</td>'.format("" if c else ' class="muted"', c or "&mdash;")
        for c in cells
    ) + "</tr>"


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard(
    last_name: str | None = Query(None, max_length=50),
    phone_number: str | None = Query(None, max_length=20),
    date_of_birth: str | None = Query(None, max_length=20),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Render the patient list, honouring the same filters as the REST API."""
    try:
        patients = patient_service.list_patients(
            session,
            last_name=last_name or None,
            phone_number=phone_number or None,
            date_of_birth=date_of_birth or None,
            limit=500,
            offset=0,
        )
        rows = [patient_service.to_dict(p) for p in patients]
        error = None
    except patient_service.ValidationError as exc:
        # A filter the validators reject (a half-typed date, say) should not
        # blank the page - say what was wrong and keep the form usable.
        rows, error = [], "; ".join(f.api_message for f in exc.failures)

    if error:
        table = '<div class="empty">{}</div>'.format(_esc(error))
    elif rows:
        table = (
            "<table><thead><tr>"
            + "".join("<th>{}</th>".format(c) for c in _COLUMNS)
            + "</tr></thead><tbody>"
            + "".join(_row(r) for r in rows)
            + "</tbody></table>"
        )
    else:
        table = '<div class="empty">No patients match those filters.</div>'

    return HTMLResponse(_PAGE.format(
        table=table,
        count="{} patient{}".format(len(rows), "" if len(rows) == 1 else "s"),
        last_name=_esc(last_name), phone_number=_esc(phone_number),
        date_of_birth=_esc(date_of_birth),
    ))
