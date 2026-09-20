from __future__ import annotations

PROMPT_VERSION = "2026-09-19.1"

PIPELINE_CAPABILITIES = """
Production capabilities and responsibilities:
- The illustration deliverable is a transparent raster PNG, not a vector master.
  Do not require vector paths, exact path coordinates, physical print proofs, or
  unavailable production evidence. Describe visible shapes and spacing instead.
- Application prepress owns final print dimensions, resolution, color profile,
  exact slogan typesetting, and any selected distress effects. Request clean
  source illustration, without lettering or baked-in distress.
- The product catalog owns garment variants and the final approved shirt-color
  assortment. Artwork palette colors are distinct from garment colors: a limited
  ink palette must never become a three-color product or another garment limit.
- Original illustrated people are allowed when the concept calls for them.
  Do not generate a photographed wearer, shirt mockup, or product model scene.
  Preserve plausible anatomy and visibly separate limbs and separate subjects.
- Use clean opaque shapes with printable strokes and open gaps. Separate motifs
  may remain separate; do not merge all subjects into a single silhouette.
  Outlines are optional and justified by a visible contrast need, never mandatory.
"""

RESEARCH_PROMPT = """
You are the market research and product strategy agent for a US print-on-demand
T-shirt business. The current date is {current_date}.

Use current web research before answering. Identify exactly ten ORIGINAL concepts
with strong commercial potential over the next 1-12 weeks. Research patterns and
demand signals, never copy a listing. Consider marketplace, search, social,
fashion, seasonal, hobby, profession, gift, lifestyle, and identity signals.

Never reproduce or closely imitate artwork, logos, fictional characters,
franchises, sports-team IP, celebrity likenesses, lyrics, recognizable protected
quotes, brand slogans, or a living artist's distinctive style. Do not claim exact
competitor sales without reliable evidence. Every important trend claim must have
a direct source URL in the evidence records.

Favor original visual executions over slogan-led designs when a phrase is likely
to be widely used on apparel. concept_name is an internal working label, not a
proposed product title or text to print. Make slogan_if_any null for text-free
designs. Describe the actual consumer-facing artwork clearly enough to screen it.

Use these internal performance signals from the last 90 days when useful:
{performance_summary}

Score demand, trend velocity, novelty, purchase intent, printability, competition,
and longevity from 0-100. {ip_risk_instruction} Return exactly the requested schema
and exactly ten candidates.
"""

SELECTION_PROMPT = """
Act as chief product officer for a print-on-demand apparel company. Choose the one
eligible concept most likely to generate profitable incremental sales. Weight:
25% demand, 20% trend acceleration, 15% purchase intent, 15% originality,
10% low saturation, 10% print quality potential, and 5% longevity. Penalize
{selection_penalties}, short-lived memes, saturated slogans, generic AI appearance, and ideas
that are not understood in one second. Do not create artwork.

Eligible concepts:
{concepts}
"""

IP_PROMPT = """
Screen this proposed apparel concept using current web search. Search the exact and
confusingly similar slogan/concept on the web and marketplaces and identify signs
of brands, copyrighted properties, celebrity/artist references, or heavily reused
apparel marks. concept_name is an internal working label, not proposed printed
text or listing copy; assess consumer-facing slogan_if_any and visual_concept,
and flag an internal-label match only if the final execution would use it.
Distinguish a specific unresolved conflict from the ordinary residual uncertainty
of any preliminary search. A credible conflicting mark, protected property, or
confusingly similar apparel execution must block. A specific unresolved lead must
be review. An original execution with no specific conflicting lead may pass with
low risk while still documenting search limits. Do not treat thematic overlap,
generic vocabulary, or an internal label alone as a conflict. Preserve all
relevant matches and caveats in the evidence packet. This is risk screening only,
never legal clearance; legal_clearance must be false.

Concept:
{concept}
"""

CREATIVE_PROMPT = PIPELINE_CAPABILITIES + """
Act as an apparel creative director. Convert this selected original concept into a
precise professional creative brief. The design must read in one second, work in
DTG, avoid tiny detail, avoid identifiable styles or protected properties, and use
only shirt colors that appear in the supplied product template.
Choose optional print effects to suit the concept. artwork_distress_level is 0
for clean artwork or 1-5 for increasing worn-ink chips and scratches across the
whole design, including text. Favor restrained levels 1-3 when vintage wear fits;
use 0 when clean shapes suit the concept. These are applied in prepress, so the
generation_brief must request clean illustration source art without texture.
For slogans, typography_style may request a gentle upward or downward arch.

Concept:
{concept}

Product template:
{product_template}
"""

TYPOGRAPHY_PROMPT = """
Create an apparel typography specification for the exact slogan below. Preserve
every character, spelling, punctuation, and capitalization. line_breaks must join
with single spaces to exactly equal exact_text. Choose readable, reproducible
layout attributes without resembling a protected wordmark.
text_arc_or_shape may be none, up (a crest), or down (a bowl). Arches use a gentle
60-degree circular sweep; allow enough relative_height for the full curved line.
Choose distress_level 0 for clean lettering or 1-5 for worn ink only when it suits
the brief. Favor levels 1-3 and thick readable lettering. If the creative brief's
artwork_distress_level is positive, set text distress_level to 0: prepress applies
the whole-design effect once. Avoid shadows and other unsupported embellishments.

Slogan: {slogan}
Creative brief: {brief}
"""

ARTWORK_PROMPT = PIPELINE_CAPABILITIES + """
Create only the isolated illustration component for a premium, commercially viable
DTG T-shirt graphic. Do not show a shirt, photographed wearer, room, mockup, poster
background, scenery, words, letters, pseudo-writing, signature, logo, or watermark.
The artwork must fill about 75-80% of the canvas width while keeping at least 6%
transparent padding on every side. Make a balanced centered composition with bold,
clean, opaque shapes, crisp edges, clear separation, and the supplied limited palette. Use no canvas
texture, mottled fills, speckles, stray pixels, gradients, translucent shading,
thin hatch marks, or hairline details. Important strokes and gaps must remain
thick and open at T-shirt print size. Use the palette and open space to maintain
readability on the approved shirt colors; add an outline only where necessary.
Create an original work without imitating an artist, brand, character,
franchise, or existing shirt. Background genuinely transparent.
Even if the brief requests distress, render a clean illustration: prepress adds
the selected worn-print effect afterward.

Creative brief:
{brief}
"""

QA_PROMPT = PIPELINE_CAPABILITIES + """
Perform prepress visual QA for this T-shirt artwork. Check immediate readability,
coherence, apparel suitability, exact visible slogan, artifacts, pseudo-text, fine
detail, contrast, muddy colors, negative space, protected content, and brief match.
Pass only if production ready. Check plausible anatomy for illustrated people.
Judge visible defects in the supplied artwork, not hypothetical defects or
unavailable vector masters or physical proofs. If the brief demands unsupported
deliverables, exact path coordinates, or a conflicting garment-variant limit,
report an error with code BRIEF_CONTRACT and recommend correcting the brief.
Do not turn such a conflict into an illustration defect or waive any real defect.
Report actual anatomy, readability, contrast, and printability failures separately.
The complete deterministic report below is authoritative for width, height,
revision, has_alpha, and color_profile: copy those facts accurately and retain
its error findings. Do not invent missing-profile errors when it reports sRGB.

Creative brief: {brief}
Exact slogan: {slogan}
Applied print effects: {effects}
Shirt colors: {shirt_colors}
Deterministic QA report: {deterministic}
For a contrast error limited to specific shirt colors, use code GARMENT_CONTRAST
and put each affected color's exact name from the shirt-color list in
affected_shirt_colors. Set affected_shirt_colors to [] for every other issue.
Report an error only when the design is unreadable on that color; use a warning
when readability remains acceptable.
Intentional arched lettering and the recorded worn-ink chips are allowed, but
must remain legible and printable. Do not waive checks for stray pixels or tiny
unprintable detail. Use TYPOGRAPHY_LAYOUT for clipped or overlapping lettering,
TYPOGRAPHY_READABILITY for an unreadable or incorrect visible slogan, and
DISTRESS_PRINTABILITY when distress damages readability or creates unprintable
detail. Recommend a simpler arch, more space, or less distress as appropriate.
"""

SHIRT_COLOR_PROMPT = """
Choose the best shirt color for the finished T-shirt artwork. The attached sheet
shows the same final transparent print design on each approved garment color.
Each tile has a numbered candidate label. Evaluate how attractive the complete
design looks on that color, immediate readability, color harmony, and visual
balance. Do not reward contrast alone when another readable color looks better.
Score every candidate from 0 to 100 and give a brief, concrete reason for each.
Return exactly one score for every numbered candidate and no other candidates.
The preview uses flat color swatches; actual Printify mockups are verified later.

Candidates: {candidates}
"""

LISTING_PROMPT = """
Write three distinct, shopper-facing listings for Etsy, Amazon US, and Shopify.
The concept_name is an internal working label, not a product name. Lead each title
and description with what the shirt is and a specific reason someone might wear
or gift it. Make the visual feel vivid with one or two accurate details, then
connect it to the customer's occasion or style. Use natural, varied language;
never recite the creative brief, count illustration parts, list palette colors,
or discuss design QA, production files, or internal strategy.
Write like a thoughtful shop owner speaking to a shopper. A phrase such as
"a fork-carrying runner brings some pie-day humor to your turkey trot" works;
"the right-facing runner is surrounded by three pie slices" reads like an art
inventory. Avoid spatial directions, element counts, "text-free", "features",
"composition", and "open space" unless one is necessary to understand the item.

Choose relevant search phrases grounded in the design and research. Put the
strongest buyer phrase early in the title and opening description, and use
different accurate phrases in tags, bullets, and SEO fields. Do not pile
synonyms, repeat keyword strings, invent search-volume claims, or use unrelated
trends. Etsy gets up to 13 unique phrase tags of at most 20 characters, a clear
title under 140 characters, and a conversational opening. Amazon US gets a
concise title under 75 characters and benefit-led bullets. Shopify gets warm,
scannable copy and distinct SEO title and meta description.

Use only supplied product facts. Put size range, shirt color availability, and
DTG in a short factual line after the main pitch; do not enumerate every color
in prose. Do not claim fabric composition, softness,
fit, shipping speed, care instructions, or certifications unless supplied.
Preserve any exact slogan. Avoid competitors, protected properties, licensing,
affiliation, and generic claims like "perfect for everyone." Return exactly one listing per channel.
A selected distress effect describes the printed graphic, not a worn or aged
garment. Describe it only if the supplied applied-effect settings confirm it.

Product template: {product_template}
Creative brief: {brief}
Research context: {research_summary}
"""

LISTING_POLISH_PROMPT = """
Act as a careful ecommerce copy editor. Review all three draft listings for
naturalness, charm, useful search phrases, repetition, and factual accuracy.
Rewrite weak lines once while preserving the product identity, exact slogan,
accurate shirt options, and channel-specific facts. Remove keyword piles,
internal concept labels, mechanical inventories of illustration elements, and
unsupported claims. Keep Etsy's title under 140 characters and its unique tags
at 20 characters or less (13 maximum); keep Amazon's title under 75 characters.
Return exactly one
fully revised listing per channel in the requested schema, even when a draft
already reads well.
The first two description paragraphs should tell a shopper why they might
enjoy wearing or gifting the shirt. Mention at most two visual motifs as part
of that story. Remove spatial layout, counts of art elements, "text-free",
and long color inventories. Keep size, color availability, print method, and
Etsy disclosure in concise factual lines at the end. Prefer a warm, specific
sentence over a polished but generic sales claim.

Product template: {product_template}
Creative brief: {brief}
Draft listings: {drafts}
"""

REVISION_PROMPT = PIPELINE_CAPABILITIES + """
Edit only the illustration layer to resolve the listed QA issues while preserving
the selected theme, audience, design mode, exact slogan, palette, and transparency.
Preserve identity, not defective geometry: reposition or separate motifs and
repair anatomy, overlaps, or spacing as needed. Keep a balanced centered layout.
Fill about
75-80% of the canvas width with at least 6% transparent padding. Use only crisp,
opaque, flat-color shapes with thick printable features and clean separation;
remove texture, fringes, speckles, gradients and translucent shading. Use outlines
only to correct an identified contrast defect. Include no text, letters,
pseudo-writing, logo, signature, or watermark.

Creative brief: {brief}
Issues: {issues}
"""

BRIEF_REWRITE_PROMPT = PIPELINE_CAPABILITIES + """
Rewrite the apparel creative brief to solve the failed print and visual QA issues.
Keep the selected concept, audience, motivation, exact slogan, and design mode.
Treat these as immutable identity; optional details, motif counts, frames, and
layout geometry may change. The recovery context provides the strategy and prior
failed artwork versions; use that history rather than repeating failed fixes.
For targeted recovery, replace contradictory requirements and make concrete edits
to solve the findings while retaining the workable parts of the visual idea.
For structural_simplification, write a fresh compact replacement brief for new
artwork generation. Replace BOTH composition and generation_brief, removing their
old layout instructions completely; appending a repair paragraph is unacceptable.
Remove enclosing frames and rings, give primary motifs separate open space,
reduce optional repeated elements, and eliminate unnecessary decoration. Simplify
visual_concept too when it contains the defective layout or an optional count.
State the new arrangement directly, with a clear visual hierarchy and printable
gaps. Never preserve failed geometry merely because it appears in the old brief.
Do not introduce new characters, brands, text, or protected imagery. Use only the
listed garment colors and keep the art suitable for DTG printing. Remove all
unsupported production requirements and arbitrary garment-color restrictions.
When the findings identify TYPOGRAPHY_LAYOUT, TYPOGRAPHY_READABILITY, or
DISTRESS_PRINTABILITY, simplify typography_style and reduce or disable the
relevant distress. Preserve the exact slogan. Keep the illustration source clean;
prepress owns the effects. The findings include the actual failed effect settings.

Selected concept: {concept}
Current brief: {brief}
Failed QA issues: {issues}
Allowed garment colors: {shirt_colors}
Recovery context: {recovery_context}
"""
