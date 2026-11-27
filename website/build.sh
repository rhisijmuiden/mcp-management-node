#!/usr/bin/env bash
# Builds the documentation site into ../site. Used by .github/workflows/docs.yml.
# Local preview: pip install -r website/requirements.txt && website/build.sh serve
set -euo pipefail
export NO_MKDOCS_2_WARNING=1
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/.." && pwd)"

# Screenshots and the generated MCP tool reference live in docs/ of the repository.
rm -rf "$here/docs/assets/images"
cp -r "$root/docs/images" "$here/docs/assets/images"
python3 - "$root/docs/mcp-tools.md" "$here/docs/en/reference/mcp-tools.md" <<'PY'
import re, sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
# Point the links of the generated file to the matching pages of this site.
text = text.replace("](features.md#ai-gateway)", "](../ai/policies-approvals.md)")
text = re.sub(r"\]\(features\.md[^)]*\)", "](../index.md)", text)
# GitHub and MkDocs make heading anchors slightly differently: map every GitHub anchor to the MkDocs one.
from markdown.extensions.toc import slugify
def github_slug(h):
    h = re.sub(r"[^\w\- ]", "", h.lower()).replace(" ", "-")
    return h.replace("_", "")
for heading in re.findall(r"^#+ (.+)$", text, flags=re.M):
    plain = heading.replace("`", "")
    text = text.replace("(#" + github_slug(plain) + ")", "(#" + slugify(plain, "-") + ")")
text = re.sub(r"\]\(\.\./([^)]*)\)", r"](https://github.com/rhisijmuiden/stackpilot/blob/main/\1)", text)
open(dst, "w", encoding="utf-8").write(text)
PY

cd "$here"
if [[ "${1:-build}" == "serve" ]]; then
  mkdocs serve
else
  mkdocs build --strict
fi
