# BoundaryTrace

**Category** · Intelligent Contracts

BoundaryTrace records a public geographic boundary once and lets other contracts compute exact route crossings against the resulting snapshot. Its first source is the U.S. Census Bureau's generalized 2025 Colorado polygon.

The leader and each GenLayer validator independently fetch the complete GeoJSON and verify the feature identity. They normalize the coordinate grid, ring start, and direction, then require exact agreement on every vertex. The contract stores the polygon under its content digest. Changes to the agreed geometry change both the snapshot ID and potentially the resulting crossings.

A read-only trace splits each submitted route segment into inside, outside, and boundary intervals. It returns crossing and tangent locations as exact reduced fractions of the segment. Multiple routes can query the same snapshot independently.

On StudioNet, two finalized observations returned the same 96-vertex snapshot. A west-to-east demo route crossed the generalized polygon twice, with exact entry and exit fractions verified against the Census fixture. GenVM lint and all six direct tests passed. Read-only replay verified the deployed source, the full polygon and trace, and all three finalized transactions.

- **Repository** · [Source, tests, and documentation](https://github.com/ehsanisildur-ux/boundary-trace)
- **StudioNet Explorer** · [Deployed contract](https://explorer-studio.genlayer.com/address/0x1E1B0e3179Dd9C42e825e042d7260C68a4aEF5c5)
- **Proofs** · [Finalized receipts, polygon, and exact crossings](https://github.com/ehsanisildur-ux/boundary-trace/blob/main/proofs/studionet.json)
