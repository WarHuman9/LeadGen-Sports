"""
test_enum_error.py (v2)

Probe: does sending an invalid categoryFilterWords value cause Apify to
return the full list of valid enum values anywhere in the error response?

WHY v1 WAS INCOMPLETE
----------------------
apify_client raises ApifyApiError, whose __str__ / str(e) only ever
returns self.message -- ONE field pulled from the response body's
`error.message`. The full response body's `error` object can also carry
a separate `data` field (self.data on the exception), which v1 never
inspected at all. So v1's "only 87362 chars" ceiling wasn't a Python
print/string truncation -- it was silently looking at the wrong field
the whole time and never seeing e.data even if it contained more.

This version captures message, type, and data as three genuinely
separate pieces of the exception, dumps EACH one to its own file (so
nothing gets truncated by print(), an f-string, or any other implicit
string-length ceiling), and reports the true full length of each. If
the real enum list exists anywhere, this will find it.

USAGE
-----
    python test_enum_error.py --apify-token <TOKEN>

Output files (written to the current directory):
    enum_probe_message.txt   -- str(e.message)
    enum_probe_type.txt      -- str(e.type)
    enum_probe_data.json     -- e.data, pretty-printed as JSON (or raw
                                 text if it isn't JSON-serializable)
    enum_probe_full_raw.txt  -- everything above concatenated, for a
                                 single grep-able file
"""

import argparse
import json
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apify-token", default=os.environ.get("APIFY_TOKEN"))
    args = parser.parse_args()
    if not args.apify_token:
        print("ERROR: --apify-token or APIFY_TOKEN env var required", file=sys.stderr)
        sys.exit(1)

    try:
        from apify_client import ApifyClient
    except ImportError:
        os.system(f"{sys.executable} -m pip install apify-client --break-system-packages -q")
        from apify_client import ApifyClient

    try:
        from apify_client.errors import ApifyApiError
    except ImportError:
        # older/newer apify-client versions may expose this differently;
        # fall back to catching the generic base Exception if the import
        # path has moved, so the probe still runs and we can inspect
        # whatever attributes the caught exception actually has.
        ApifyApiError = Exception  # type: ignore[assignment,misc]

    client = ApifyClient(args.apify_token)

    print("Sending a deliberately invalid categoryFilterWords value...")
    try:
        client.actor("compass/crawler-google-places").call(
            run_input={
                "searchStringsArray": ["test"],
                "locationQuery": "New York, US",
                "categoryFilterWords": ["THIS_IS_DEFINITELY_NOT_A_VALID_CATEGORY_XYZ123"],
                "maxCrawledPlacesPerSearch": 1,
            }
        )
        print("⚠️ No error raised -- actor accepted the bad value silently. "
              "categoryFilterWords may not be validated as a strict enum at the API layer, "
              "meaning this whole extraction approach won't work -- see the fallback note below.")
        return

    except Exception as e:
        message = getattr(e, "message", None)
        etype = getattr(e, "type", None)
        data = getattr(e, "data", None)
        status_code = getattr(e, "status_code", None)

        message_str = str(message) if message is not None else ""
        type_str = str(etype) if etype is not None else ""

        # data might be a dict, list, string, or None -- handle generically
        if data is None:
            data_str = ""
            data_json_ok = False
        elif isinstance(data, (dict, list)):
            try:
                data_str = json.dumps(data, indent=2, ensure_ascii=False)
                data_json_ok = True
            except TypeError:
                data_str = str(data)
                data_json_ok = False
        else:
            data_str = str(data)
            data_json_ok = False

        with open("enum_probe_message.txt", "w", encoding="utf-8") as f:
            f.write(message_str)
        with open("enum_probe_type.txt", "w", encoding="utf-8") as f:
            f.write(type_str)
        with open("enum_probe_data.json" if data_json_ok else "enum_probe_data.txt", "w", encoding="utf-8") as f:
            f.write(data_str)
        with open("enum_probe_full_raw.txt", "w", encoding="utf-8") as f:
            f.write(f"status_code: {status_code}\n\n")
            f.write(f"=== message ({len(message_str)} chars) ===\n{message_str}\n\n")
            f.write(f"=== type ({len(type_str)} chars) ===\n{type_str}\n\n")
            f.write(f"=== data ({len(data_str)} chars) ===\n{data_str}\n")

        print(f"\nException type      : {type(e).__name__}")
        print(f"HTTP status_code     : {status_code}")
        print(f"e.message length     : {len(message_str)} chars  -> enum_probe_message.txt")
        print(f"e.type length        : {len(type_str)} chars  -> enum_probe_type.txt")
        print(f"e.data length        : {len(data_str)} chars  -> enum_probe_data.{'json' if data_json_ok else 'txt'}")
        print("Combined dump also written to enum_probe_full_raw.txt\n")

        # crude heuristic check across ALL three fields, not just message
        combined = message_str + type_str + data_str
        comma_count = combined.count(",")
        if comma_count > 100:
            print(f"✅ Combined fields contain {comma_count} commas -- looks like a large "
                  f"enumerated list IS present somewhere in the response. Check the .txt/.json "
                  f"files above (enum_probe_data is the most likely place for a structured list).")
        else:
            print(f"❌ Combined fields only contain {comma_count} commas -- doesn't look like "
                  f"a full enum list is present in this exception's message/type/data at all.\n"
                  f"   This would mean the API validates categoryFilterWords server-side without\n"
                  f"   echoing the valid-values list back on failure. In that case the enum list\n"
                  f"   needs to come from Apify's/Compass's own schema documentation instead --\n"
                  f"   see the next step suggested in chat.")


if __name__ == "__main__":
    main()