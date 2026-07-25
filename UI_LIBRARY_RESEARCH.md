# UI library research

Checked 2026-07-24. Reverify versions and maintenance before adopting anything.

## Recommendation

Do not add a runtime UI library during the rendering-parity cleanup. The agent
timeline is a nested causal feed, so generic timeline/chart packages do not
replace its custom event normalization or presentation.

If manual shell-component CSS becomes the next maintenance bottleneck, pilot a
small pinned and locally vendored subset of
[Open Props UI](https://open-props-ui.netlify.app/html/guide/getting-started/).
Buttons, badges, cards, native details/accordions, and tooltips are reasonable
targets. Keep the timeline itself custom and keep the navy/blush tokens in
`static/theme.css`.

For lower risk, copy only selected
[Open Props](https://open-props.style/) modules for spacing, radii, shadows, and
easing. This regularizes CSS but does not reduce JavaScript DOM construction.

If `createElement` boilerplate is still a problem after the shared render model
settles, evaluate standalone [Lit templates](https://lit.dev/docs/templates/overview/)
inside the timeline renderer. Do not introduce LitElement, Shadow DOM, or a
component hierarchy just for this app.

## Candidate fit

| Candidate | Approx. gzip | License/model | Fit for pi chat |
|---|---:|---|---|
| [Open Props UI](https://github.com/felix-bohlin/ui) | 32.8 KB full; ~6.9 KB for base + 5 components | MIT, opt-in CSS classes | Best narrow shell-component pilot. Its copyable component CSS preserves plain HTML and avoids a runtime dependency. |
| [Open Props](https://github.com/argyleink/open-props) | 7.7 KB full tokens; modules are smaller | MIT, modular CSS custom properties | Lowest-risk token source. Use a curated subset so the vocabulary stays small. |
| [Lit](https://lit.dev/docs/getting-started/) | ~6.1 KB core browser bundle | BSD-3-Clause, template runtime | Best future option for declarative timeline templates, but it styles nothing and should follow—not lead—event normalization. |
| [Pico CSS](https://picocss.com/docs/sass) | ~11.7 KB full CSS | MIT, classless/modular CSS | Not recommended here. Global element rules are likely to collide with the theme, Markdown, and timeline. |
| [Alpine.js](https://alpinejs.dev/) | ~16.7 KB browser build | MIT, HTML directives plus reactive runtime | Useful for simple toggles, but splitting streaming state between directives and imperative event code would make this app harder to follow. |
| [Web Awesome](https://webawesome.com/docs/usage/) | ~55–65 KB for a small component closure, plus assets | MIT free components, Lit-based Web Components | Too much module, asset, Shadow DOM, and theming surface for this local app. |
| [Basecoat](https://basecoatui.com/installation/) | Depends on generated Tailwind output | MIT, Tailwind-generated components | Not recommended because it adds a build pipeline and Tailwind vocabulary. |

Sizes were measured with `gzip -9` from official browser/npm artifacts and are
only comparison estimates, not hard budgets.

The published Open Props UI package used a nonstandard `"catalog:"` peer
dependency declaration when checked, so its documented copy-component-CSS path
is safer than adding it through npm. Also note that
[Shoelace](https://github.com/shoelace-style/shoelace) was archived; do not
choose it as a lighter Web Awesome alternative.

## Adoption rules

- Vendor exact versions locally; the application should not require a CDN.
- Add one narrow component family at a time and compare both themes.
- Keep runtime dependencies at zero unless a measured maintenance problem
  justifies one.
- Do not add a bundler, state manager, virtual DOM, or utility-class framework
  for this UI.
- The external font stylesheet in `static/index.html` is the remaining
  runtime CDN dependency and should be vendored or removed if fully offline
  startup becomes a requirement.
