# Data Display And Dashboards

| Component | Use for | Notes |
|---|---|---|
| Table | simple tabular data | Good when sorting/filtering/pagination are not complex. |
| Data Table | rich table pattern | Pattern/example using TanStack Table; verify dependencies. |
| Chart | visual metrics | Check chart library dependencies and responsive containers. |
| Avatar | people/entities | Always provide fallback. |
| Item | reusable item row/content pattern | Good for dense lists and command-like layouts. |

For dashboards, combine Sidebar, Card, Chart, Table/Data Table, Skeleton, Empty,
and Alert with stable dimensions to avoid loading shifts.
