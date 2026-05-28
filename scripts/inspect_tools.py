#!/usr/bin/env python3
"""Print the Gemini function declarations generated from ALL_TOOLS.

Run this to verify exactly what schema Gemini receives for each tool —
no API key or network access required.

Usage:
    python scripts/inspect_tools.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.genai import types

from docktalk.agent.tools import ALL_TOOLS


def main() -> None:
    print(f"Inspecting {len(ALL_TOOLS)} tools\n{'=' * 50}")

    for fn in ALL_TOOLS:
        decl = types.FunctionDeclaration.from_callable_with_api_option(
            callable=fn, api_option="GEMINI_API"
        )

        print(f"\n{fn.__name__}")
        print(f"  description: {decl.description!r}")

        if not decl.parameters or not decl.parameters.properties:
            print("  params:      (none)")
        else:
            for name, schema in decl.parameters.properties.items():
                required = (
                    name in (decl.parameters.required or [])
                )
                print(
                    f"  param  [{name}]  type={schema.type}  "
                    f"required={required}  description={schema.description!r}"
                )


if __name__ == "__main__":
    main()
