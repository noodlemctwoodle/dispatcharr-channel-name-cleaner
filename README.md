# Dispatcharr — Channel Name Cleaner

A Dispatcharr plugin that tidies messy IPTV channel names.

**Rules (defaults are conservative):** strip pipe-delimited junk & quality tags
(`Rte One +1 | HD |` → `Rte One +1`), strip fancy-unicode HD marks (`007 ᴴᴰ` → `007`),
collapse whitespace. Riskier rules (`+1` spacing, title-casing) are OFF by default.

**Safety:** per-group scope, a protect-list (foreign-script names, `&TV`-style names,
event/info groups, custom regex), collision detection (`Channel.name` is unique, so a
clean that would duplicate an existing name is **skipped**, not forced), a backup CSV on
Apply and a one-click **Undo Last**.

**Actions:** Preview (dry run) → Apply → Undo Last → Last Results.

Install: copy the `channel_name_cleaner/` folder into Dispatcharr's `/data/plugins/`
then reload plugins (or restart Dispatcharr). Configure + run under **Plugins → My Plugins**.
