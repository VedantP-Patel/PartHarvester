"""Frontend Static Analysis and DOM Integrity Test Suite.

Ensures JavaScript syntax is 100% valid, no rogue braces exist, and every DOM
element ID referenced in web/app.js exists in web/index.html.
"""

import os
import re
import shutil
import subprocess
import pytest

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
APP_JS_PATH = os.path.join(WEB_DIR, "app.js")
INDEX_HTML_PATH = os.path.join(WEB_DIR, "index.html")


def test_app_js_syntax():
    """Validates web/app.js syntax using Node.js check_syntax if node is available."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js is not installed; skipping JS syntax compilation check.")

    try:
        result = subprocess.run(
            [node_bin, "-c", APP_JS_PATH],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"web/app.js syntax error:\n{result.stderr}"
    except OSError:
        result = subprocess.run(
            [node_bin, "-c", APP_JS_PATH],
        )
        assert result.returncode == 0, "web/app.js syntax error"


def test_dom_ids_integrity():
    """Verifies that every document.getElementById target in app.js exists in index.html."""
    assert os.path.isfile(APP_JS_PATH), "web/app.js missing"
    assert os.path.isfile(INDEX_HTML_PATH), "web/index.html missing"

    with open(APP_JS_PATH, "r", encoding="utf-8") as f:
        js_content = f.read()

    with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
        html_content = f.read()

    # Extract all document.getElementById calls in app.js
    js_ids = set(re.findall(r"document\.getElementById\(['\"]([^'\"]+)['\"]\)", js_content))

    # Extract all id="..." attributes in index.html
    html_ids = set(re.findall(r'id=[\'"]([^\'"]+)[\'"]', html_content))

    # Check for missing elements
    missing_ids = js_ids - html_ids
    assert not missing_ids, f"The following DOM IDs are referenced in app.js but missing in index.html: {missing_ids}"


def test_critical_controls_present():
    """Verifies all mission-critical buttons and views exist in index.html."""
    with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
        html_content = f.read()

    critical_ids = [
        "adapters-list",
        "btn-add-links",
        "links-input",
        "tasks-table-body",
        "total-tasks-count",
        "toast-container",
        "ws-dot",
        "ws-status",
    ]

    for cid in critical_ids:
        assert f'id="{cid}"' in html_content, f"Critical ID '{cid}' is missing from index.html"

