So the response includes the **```json ... ``` code fences**, which means:

- `content` = the whole fenced block  
- `json.loads(content)` → **raises an error**  
- You fall into the `except` block in `generate_ai_fields`  
  - `us_macro`, `macro_event`, `reg_or_news_1`, `reg_or_news_2` revert to the **static fallback lines**  
  - `ai_comment` becomes the **raw content** (the whole JSON blob), which is why you see the JSON printed under “AI Market Take”

So everything is technically working, but the parser is choking on the backticks.

Let’s fix that properly.  

---

## 🔧 Fix: Strip code fences and extract the JSON before parsing

You only need to change **`generate_ai_fields`** to be more robust:

1. Strip ```json fences if present  
2. If that fails, grab the substring between the first `{` and the last `}`  
3. Then run `json.loads()` on that clean string

Here’s a drop-in replacement for your `generate_ai_fields` plus a small helper `_extract_json_object`:

```python
def _extract_json_object(content: str) -> str:
    """
    Try to extract a JSON object from the model output.
    Handles cases where the model wraps JSON in ```json ... ``` fences.
    """
    content = content.strip()

    # Strip markdown code fences if present
    if content.startswith("```"):
        # Remove leading ```
        content = content.lstrip("`")
        # After lstrip, there might be 'json' or 'JSON' etc.
        first_brace = content.find("{")
        last_brace = content.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            return content[first_brace:last_brace + 1].strip()

    # Generic: take the first {...} block in the string
    first_brace = content.find("{")
    last_brace = content.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return content[first_brace:last_brace + 1].strip()

    # Fallback: return as-is (let json.loads fail and upper layer handle)
    return content


def generate_ai_fields(metrics: dict) -> dict:
    """
    Use OpenAI to generate:
    - us_macro
    - macro_event
    - reg_or_news_1
    - reg_or_news_2
    - ai_comment (market take)

    Returns a dict with those keys.
    """

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        log.warning("CryptoWatch: OPENAI_API_KEY not set, skipping AI analysis.")
        return {
            "us_macro": "Cautious ahead of U.S. data and Fed speakers.",
            "macro_event": "Key U.S. data + Fed commentary on rates/inflation.",
            "reg_or_news_1": "Watching exchange + stablecoin oversight developments.",
            "reg_or_news_2": "Some pressure around DeFi and offshore venues.",
            "ai_comment": "AI analysis disabled (no API key configured).",
        }

    try:
        client = OpenAI(api_key=api_key)
        model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

        # Prepare numeric-ish values for context
        fg_val_str = str(metrics.get("fg_value"))
        fg_label_str = str(metrics.get("fg_label"))

        btc_price_str = metrics.get("btc_price", "N/A")
        eth_price_str = metrics.get("eth_price", "N/A")

        btc_24h = metrics.get("btc_24h")
        eth_24h = metrics.get("eth_24h")
        btc_24h_str = _fmt_pct(btc_24h) if isinstance(btc_24h, (int, float)) else "N/A"
        eth_24h_str = _fmt_pct(eth_24h) if isinstance(eth_24h, (int, float)) else "N/A"

        total_mc_val = metrics.get("total_mc")
        total_mc_24h = metrics.get("total_mc_24h")
        if isinstance(total_mc_val, (int, float)):
            total_mc_str = f"${total_mc_val/1e12:.2f}T"
        else:
            total_mc_str = "N/A"
        total_mc_24h_str = _fmt_pct(total_mc_24h) if isinstance(total_mc_24h, (int, float)) else "N/A"

        dxy_val = metrics.get("dxy_value")
        dxy_pct = metrics.get("dxy_change_24h")
        dxy_val_str = str(dxy_val) if isinstance(dxy_val, (int, float)) else "N/A"
        dxy_pct_str = _fmt_pct(dxy_pct) if isinstance(dxy_pct, (int, float)) else "N/A"

        spx_val = metrics.get("spx_fut")
        spx_pct = metrics.get("spx_fut_pct")
        spx_val_str = f"{spx_val:,.0f}" if isinstance(spx_val, (int, float)) else "N/A"
        spx_pct_str = _fmt_pct(spx_pct) if isinstance(spx_pct, (int, float)) else "N/A"

        data_snippet = (
            f"Date: {datetime.utcnow().date().isoformat()}\n"
            f"Sentiment: {metrics['sentiment']}\n"
            f"Fear & Greed: {fg_val_str} ({fg_label_str})\n"
            f"BTC: {btc_price_str} ({btc_24h_str}% / 24h)\n"
            f"ETH: {eth_price_str} ({eth_24h_str}% / 24h)\n"
            f"Total Market Cap: {total_mc_str} ({total_mc_24h_str}%)\n"
            f"Funding: {metrics['funding_rate']}\n"
            f"Open Interest 24h: {metrics['oi_change_24h']}%\n"
            f"Liquidations 12h: Longs {metrics['liq_long']} / Shorts {metrics['liq_short']}\n"
            f"Macro (DXY/SPX if known): DXY {dxy_val_str} ({dxy_pct_str}%), "
            f"SPX Futures {spx_val_str} ({spx_pct_str}%)\n"
        )

        system_msg = (
            "You are a professional crypto and macro trader. "
            "You write short, high-signal market briefs for other traders. "
            "Be concise, actionable, and avoid explicit financial advice. "
            "Respond ONLY with valid JSON, no extra text."
        )

        user_msg = (
            "Using the data below, produce a JSON object with the following keys:\n"
            "  us_macro: one short sentence describing the current U.S. macro mood.\n"
            "  macro_event: one short phrase describing the most important event today.\n"
            "  reg_or_news_1: one short line about regulation / policy / crypto news tone.\n"
            "  reg_or_news_2: one additional short line about regulation or structural themes.\n"
            "  ai_comment: a 3–6 sentence market take for crypto traders before the U.S. cash session.\n"
            "The ai_comment should discuss:\n"
            "- overall risk mood (risk-on/off)\n"
            "- BTC/ETH context\n"
            "- how macro tone might influence flows\n"
            "- what kind of day to expect (choppy, trending, squeeze risk, etc.)\n\n"
            "IMPORTANT: Return ONLY JSON, like:\n"
            "{\n"
            '  \"us_macro\": \"...\",\n'
            '  \"macro_event\": \"...\",\n'
            '  \"reg_or_news_1\": \"...\",\n'
            '  \"reg_or_news_2\": \"...\",\n'
            '  \"ai_comment\": \"...\"\n'
            "}\n\n"
            f"DATA:\n{data_snippet}"
        )

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.7,
            max_tokens=600,
        )

        content = resp.choices[0].message.content.strip()
        json_str = _extract_json_object(content)

        try:
            obj = json.loads(json_str)
        except Exception as e:
            log.error(
                "CryptoWatch: failed to parse AI JSON: %s | raw=%r | extracted=%r",
                e, content, json_str
            )
            # Fallback: treat entire content as ai_comment
            return {
                "us_macro": "Cautious ahead of U.S. data and Fed speakers.",
                "macro_event": "Key U.S. data + Fed commentary on rates/inflation.",
                "reg_or_news_1": "Watching exchange + stablecoin oversight developments.",
                "reg_or_news_2": "Some pressure around DeFi and offshore venues.",
                "ai_comment": content or "AI analysis temporarily unavailable.",
            }

        us_macro = obj.get("us_macro") or "Cautious ahead of U.S. data and Fed speakers."
        macro_event = obj.get("macro_event") or "Key U.S. data + Fed commentary on rates/inflation."
        reg1 = obj.get("reg_or_news_1") or "Watching exchange + stablecoin oversight developments."
        reg2 = obj.get("reg_or_news_2") or "Some pressure around DeFi and offshore venues."
        ai_comment = obj.get("ai_comment") or "AI analysis temporarily unavailable."

        return {
            "us_macro": us_macro,
            "macro_event": macro_event,
            "reg_or_news_1": reg1,
            "reg_or_news_2": reg2,
            "ai_comment": ai_comment,
        }

    except Exception as e:
        log.error("CryptoWatch: AI generation failed: %s", e)
        return {
            "us_macro": "Cautious ahead of U.S. data and Fed speakers.",
            "macro_event": "Key U.S. data + Fed commentary on rates/inflation.",
            "reg_or_news_1": "Watching exchange + stablecoin oversight developments.",
            "reg_or_news_2": "Some pressure around DeFi and offshore venues.",
            "ai_comment": "AI analysis temporarily unavailable.",
        }