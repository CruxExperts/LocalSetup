# Overlays And Menus

| Component | Use for | Notes |
|---|---|---|
| Dialog | focused modal task | Requires title; test focus return. |
| Alert Dialog | destructive or critical confirm | Use for irreversible actions, not ordinary choices. |
| Sheet | side panel | Good for responsive navigation or secondary workflows. |
| Drawer | bottom/mobile drawer | Prefer for mobile-heavy flows. |
| Popover | contextual floating content | Do not use for critical confirmations. |
| Hover Card | hover/focus preview | Content must not be essential. |
| Tooltip | terse hints | Do not replace labels. |
| Dropdown Menu | action menu | Items stay in menu groups. |
| Context Menu | right-click/long-press menu | Provide alternate access when needed. |
| Menubar | app-like command bar | Use sparingly; keyboard behavior matters. |
| Command | command palette/search list | Often composed inside Dialog. |

Avoid custom portal and z-index changes until the local bug is understood.
