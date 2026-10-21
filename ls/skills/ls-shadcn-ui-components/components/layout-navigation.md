# Layout And Navigation

| Component | Use for | Notes |
|---|---|---|
| Card | grouped content | Avoid nesting cards unless repeating items require it. |
| Breadcrumb | location hierarchy | Keep labels short and links meaningful. |
| Navigation Menu | app/site navigation | Test keyboard behavior and responsive behavior. |
| Pagination | paged lists | Include current page state and disabled controls. |
| Sidebar | app shell navigation | Often uses provider/layout patterns; inspect generated code. |
| Tabs | same-page sections | Triggers stay inside `TabsList`. |
| Resizable | split panes | Ensure keyboard and min-size behavior. |
| Scroll Area | styled scroll containers | Do not hide essential content. |
| Separator | visual separation | Use semantic orientation when meaningful. |
| Accordion | collapsible sections | Good for progressive disclosure; avoid for primary navigation. |
| Collapsible | simple hide/show | Provide clear trigger state. |
