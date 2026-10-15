"""OpsResolve public portfolio demo (the entrypoint for a hosted deployment).

Run from the repository root, bound to this computer:

    pip install -e ".[app]"
    streamlit run demo/streamlit_app.py --server.address localhost

On a hosting platform (Streamlit Community Cloud), this file is the main file; the
platform binds the server and reads the repository's single .streamlit/config.toml.

This is the local app (``app.py``) in its public-demo mode: visitors can run the
bundled synthetic sample and explore the results, but no file can be uploaded, AI
explanations are not offered (their readiness is never checked, so a configured
key is never read) and error details are hidden. Each browser session keeps its
own results in Streamlit's session state; nothing is shared between visitors or
written to disk outside the per-run temporary folder.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # app.py lives in the repository root, next to this folder
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402

app.main(public_demo=True)
