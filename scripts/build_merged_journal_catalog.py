"""Build merged 四列表格 from all xlsx under 期刊列表分组/期刊列表分组."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parents[1] / "期刊列表分组" / "期刊列表分组"
OUT = Path(__file__).resolve().parents[1] / "期刊列表分组" / "期刊列表合并.xlsx"

# 输出顺序：与 README 大类相近，其余按英文名；工作论文置末
FILE_ORDER = [
    "aea_journals_7.xlsx",
    "aom_journals_2.xlsx",
    "cambridge_journals_9.xlsx",
    "degruyter_journals_2.xlsx",
    "informs_journals_6.xlsx",
    "mit_press_journals_2.xlsx",
    "oup_journals_23.xlsx",
    "sage_journals_20.xlsx",
    "sciencedirect_journals_45.xlsx",
    "springer_journals_19.xlsx",
    "tandfonline_journals_15.xlsx",
    "uchicago_journals_9.xlsx",
    "uwpress_journals_2.xlsx",
    "wiley_journals_50.xlsx",
    "other_journals_14.xlsx",
    "working_papers_1.xlsx",
]

PUBLISHER_BY_FILE: dict[str, str] = {
    "aea_journals_7.xlsx": "美国经济学会 (AEA)",
    "aom_journals_2.xlsx": "Academy of Management",
    "cambridge_journals_9.xlsx": "Cambridge University Press",
    "degruyter_journals_2.xlsx": "De Gruyter",
    "informs_journals_6.xlsx": "INFORMS",
    "mit_press_journals_2.xlsx": "MIT Press",
    "other_journals_14.xlsx": "其他 / 散刊",
    "oup_journals_23.xlsx": "Oxford University Press",
    "sage_journals_20.xlsx": "SAGE Publications",
    "sciencedirect_journals_45.xlsx": "Elsevier / ScienceDirect",
    "springer_journals_19.xlsx": "Springer Nature",
    "tandfonline_journals_15.xlsx": "Taylor & Francis",
    "uchicago_journals_9.xlsx": "University of Chicago Press",
    "uwpress_journals_2.xlsx": "University of Wisconsin Press",
    "wiley_journals_50.xlsx": "Wiley",
    "working_papers_1.xlsx": "工作论文",
}


def main() -> None:
    rows: list[dict[str, str]] = []
    for fname in FILE_ORDER:
        path = BASE / fname
        if not path.is_file():
            raise SystemExit(f"Missing expected file: {path}")
        pub = PUBLISHER_BY_FILE.get(fname)
        if not pub:
            raise SystemExit(f"No publisher label for {fname}")
        df = pd.read_excel(path, sheet_name=0)
        if "期刊" not in df.columns or "网址1 current issue" not in df.columns:
            raise SystemExit(f"Bad columns in {fname}: {list(df.columns)}")
        df = df.sort_values("ID", na_position="last")
        for _, r in df.iterrows():
            title = str(r["期刊"]).strip() if pd.notna(r["期刊"]) else ""
            url = str(r["网址1 current issue"]).strip() if pd.notna(r["网址1 current issue"]) else ""
            if not title:
                continue
            rows.append({"出版社": pub, "期刊": title, "网址": url})

    out_df = pd.DataFrame(rows)
    out_df.insert(0, "编号", range(1, len(out_df) + 1))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(OUT, engine="openpyxl") as w:
        out_df.to_excel(w, sheet_name="Sheet1", index=False)
    print(f"Wrote {OUT} ({len(out_df)} rows)")


if __name__ == "__main__":
    main()
