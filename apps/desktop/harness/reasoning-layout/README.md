# Reasoning layout harness

This fixture starts from a persisted, tool-turn-shaped assistant record: canonical `content` is empty, two native Codex reasoning items contain the same four ordered `summary_text` records, and one explicit assistant `message` item has phase `commentary` with the text `I’m checking the persisted turn now.`. The flattened `reasoning` field is the structured reasoning canonical form followed by that commentary after `\n\n`.

The fixture passes the persisted record through production `toChatMessages`, then renders the resulting production `Thread` with production styles. It does not recreate the UI or its CSS. This is supporting layout proof for commentary hydration; it is **not R3**.

From `apps/desktop`:

```bash
npm run harness:reasoning
# In another terminal:
npm run harness:reasoning:assert
```

The assertion expands the real Thought disclosure and preserves the existing proof that all four native reasoning summaries render as `display: block` at four distinct vertical positions. It additionally fails unless:

- the four disclosure-scoped reasoning nodes have the exact expected summary texts in order;
- the exact commentary text is visible once in one assistant `.aui-md` text block;
- the disclosure contains no commentary and the commentary block is not its descendant;
- the commentary rectangle starts at or below the disclosure rectangle's bottom and the rectangles do not overlap;
- one additional reasoning part renders three blank-line-separated Markdown paragraphs at the same compact pitch as native summary rows, without overlap;
- an ordinary assistant Markdown control renders three blank-line-separated paragraphs with the unchanged `--paragraph-gap` rhythm.

Successful JSON reports the reasoning rows, disclosure rectangle, commentary count/text/display/rectangle, and explicit outside, below, and non-overlap flags. Pass a URL and optional screenshot path directly when using a non-default port; supplying the path writes the screenshot:

```bash
node harness/reasoning-layout/assert.mjs http://127.0.0.1:5574/harness/reasoning-layout/ reasoning-layout.png
```
