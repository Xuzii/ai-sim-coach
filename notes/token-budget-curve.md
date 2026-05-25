# The token-budget design and tradeoff curve

**Thesis:** A raw 50 Hz, multi-channel lap trace is tens of thousands of tokens —
one tool call would exhaust the context window. So tools default to summary stats
and expose a `detail_level` knob (`summary` | `low` 5 Hz | `medium` 10 Hz |
`full` 50 Hz), and traces are binned by distance.

To capture while building:
- [ ] Token count of each tool's response at each `detail_level`.
- [ ] The tradeoff curve: detail vs tokens vs answer quality — the plot for the
      blog post.
- [ ] Where the knee is: the lowest detail that still answers the 5 demo queries
      correctly.
