from __future__ import annotations
import json, os


def save_site_json(output_dir: str, site: str, jobs: list[dict]) -> str:
    os.makedirs(output_dir, exist_ok=True)
    out = os.path.join(output_dir, f"{site}_job_postings.json")
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=4)
    os.replace(tmp, out)
    print(f"[save] {out}")
    return out
