#!/usr/bin/env python3
"""
add_catphan_nav.py
------------------
Run this once from your project root to add the CatPhan 604 nav item
to every existing frontend HTML file that has the sidebar.

Usage:
    python add_catphan_nav.py
"""

import os
import glob

# The nav item to insert (after congruence.html link)
NEW_NAV_ITEM = '    <a class="nav-item" href="catphan.html"><span class="nav-icon">⊙</span> CatPhan 604</a>\n'

# Anchor: insert AFTER this line
ANCHOR = 'href="congruence.html"'

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")
html_files   = glob.glob(os.path.join(FRONTEND_DIR, "*.html"))

patched = 0
for fpath in html_files:
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()

    # Skip if already patched or no sidebar
    if "catphan.html" in content or ANCHOR not in content:
        continue

    # Find the line with the congruence link and insert after it
    lines     = content.splitlines(keepends=True)
    new_lines = []
    for line in lines:
        new_lines.append(line)
        if ANCHOR in line and NEW_NAV_ITEM.strip() not in line:
            new_lines.append(NEW_NAV_ITEM)

    with open(fpath, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    print(f"  ✓ Patched: {os.path.basename(fpath)}")
    patched += 1

print(f"\nDone — {patched} file(s) updated.")
