"""Auditable one-product-per-category curation for the synced Printify catalog."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from merch.database import session_scope
from merch.repository import CatalogRepository
from merch.schemas import CatalogProduct

ALGORITHM_VERSION = "quality-v3-printify-ranked"
PRINTIFY_CHOICE_PROVIDER_ID = 99
POPULARITY_SOURCE = "Printify live Bestsellers blueprint ranking"


@dataclass(frozen=True)
class CategoryRule:
    slug: str
    display_name: str
    patterns: tuple[str, ...]


# Rules are intentionally ordered from specific to broad. Product titles are the only
# stable category signal stored by Printify's public catalog API.
CATEGORY_RULES = (
    CategoryRule(
        "pet-product", "Pet product", (r"\bpet\b", r"\bdog\b", r"\bcat\b", r"\bpet tag\b")
    ),
    CategoryRule(
        "baby-clothing",
        "Baby clothing",
        (
            r"\bbaby bodysuit\b",
            r"\bbaby.*bodysuit\b",
            r"\binfant\b",
            r"\bonesie\b",
            r"\bbaby.*(?:tee|shirt|dress|romper)",
            r"\btoddler.*(?:tee|shirt|dress|romper)",
        ),
    ),
    CategoryRule(
        "baby-accessory",
        "Baby accessory",
        (r"\bbaby bib\b", r"\bswaddle\b", r"\bburp cloth\b", r"\bdiaper cover\b"),
    ),
    CategoryRule(
        "kids-home",
        "Baby and kids home essential",
        (r"\bchanging pad\b", r"\bcrib\b", r"\bkids? (?:blanket|pillow|towel)\b"),
    ),
    CategoryRule(
        "kids-apparel",
        "Kids apparel",
        (
            r"\b(?:kids?|youth|children'?s|girls?|boys?)\b.*\b(?:tee|t-shirt|shirt|hoodie|sweatshirt|jersey|shorts?|leggings?|jacket)\b",
        ),
    ),
    CategoryRule(
        "phone-case",
        "Phone case",
        (
            r"\bphone case\b",
            r"\biphone.*case\b",
            r"\bsamsung.*case\b",
            r"\bbiodegradable cases?\b",
            r"\b(?:clear|tough|colorful|impact-resistant) cases?\b",
            r"\bphone cases?\b",
            r"\b(?:flexi|flip|slim|snap|magnetic) cases?\b",
            r"\bmagnetic clear impact cases?\b",
        ),
    ),
    CategoryRule(
        "laptop-tablet-sleeve",
        "Laptop or tablet sleeve",
        (
            r"\blaptop (?:sleeve|case)\b",
            r"\btablet (?:sleeve|case)\b",
            r"\bipad (?:sleeve|case)\b",
            r"\bmacbook cases?\b",
            r"\bkindle case\b",
            r"\btablet folio\b",
        ),
    ),
    CategoryRule(
        "mouse-pad",
        "Mouse pad",
        (r"\bmouse ?(?:pad|mat)\b", r"\bdesk mats?\b", r"\bgaming mat\b"),
    ),
    CategoryRule(
        "tech-accessory",
        "Tech accessory",
        (
            r"\bairpods?\b",
            r"\bearbuds?\b",
            r"\bcharger\b",
            r"\bbluetooth\b",
            r"\bspeaker\b",
            r"\bkeyboard\b",
            r"\bpower bank\b",
            r"\bphone stand\b",
            r"\bpowerbank\b",
            r"\bdisplay stand for smartphones\b",
            r"\bphone (?:click-on )?grip\b",
            r"\bphone skin\b",
            r"\bphone sanitizer\b",
            r"\bwatch band\b",
        ),
    ),
    CategoryRule(
        "car-accessory",
        "Car accessory",
        (
            r"\bcar (?:sun shade|mat|seat cover|air freshener)\b",
            r"\bcar (?:sun shades|mats|seat covers|magnets)\b",
            r"\blicense plate\b",
            r"\bsteering wheel\b",
            r"\bbumper stickers?\b",
            r"\bvanity plate\b",
        ),
    ),
    CategoryRule(
        "travel-accessory",
        "Travel accessory",
        (
            r"\bpassport\b",
            r"\bluggage tag\b",
            r"\bluggage tags\b",
            r"\bluggage cover\b",
            r"\btravel (?:case|pouch|wallet|bag)\b",
            r"\bneck pillow\b",
        ),
    ),
    CategoryRule(
        "jewelry",
        "Jewelry",
        (
            r"\bnecklace\b",
            r"\bbracelet\b",
            r"\bearrings?\b",
            r"\bring\b(?!\s+spun)",
            r"\bpendant\b",
            r"\bjewelry\b",
            r"\bcharm\b",
            r"\bcufflinks?\b",
            r"\banklet\b",
        ),
    ),
    CategoryRule("face-mask", "Face mask", (r"\bface (?:mask|cover)\b", r"\bneck gaiter\b")),
    CategoryRule(
        "hat",
        "Hat",
        (
            r"\bhat\b",
            r"\bcap\b",
            r"\bbeanie\b",
            r"\bvisor\b",
            r"\bsnapback\b",
            r"\bbalaclava\b",
            r"\btrucker\b",
            r"\bflatbill\b",
            r"\b[57]-panel\b",
            r"\bcasual performance lite\b",
            r"\bpeached cotton twill\b",
        ),
    ),
    CategoryRule("socks", "Socks", (r"\bsocks?\b",)),
    CategoryRule(
        "underwear",
        "Underwear",
        (r"\bunderwear\b", r"\bboxer(?:s| briefs)?\b", r"\bbriefs?\b", r"\bpanties\b"),
    ),
    CategoryRule(
        "bag",
        "Bag",
        (
            r"\bbag\b",
            r"\bbackpack\b",
            r"\btote\b",
            r"\bduffel\b",
            r"\bfanny pack\b",
            r"\bpouch\b",
            r"\bclutch\b",
            r"\bwallet\b",
            r"\bhandbags?\b",
            r"\bcrossbody\b",
            r"\bsling\b",
            r"\bsack\b",
            r"\bsuitcase\b",
        ),
    ),
    CategoryRule(
        "shoes",
        "Shoes",
        (
            r"\bshoes?\b",
            r"\bsneakers?\b",
            r"\bboots?\b",
            r"\bsandals?\b",
            r"\bflip[- ]flops?\b",
            r"\bclogs?\b",
            r"\bslippers?\b",
        ),
    ),
    CategoryRule(
        "swimwear",
        "Swimwear",
        (r"\bswimsuit\b", r"\bbikini\b", r"\bswim (?:trunks|shorts)\b", r"\bone-piece swimsuit\b"),
    ),
    CategoryRule(
        "sleepwear",
        "Sleepwear",
        (r"\bpajamas?\b", r"\bpajama set\b"),
    ),
    CategoryRule(
        "dress-skirt",
        "Dress or skirt",
        (r"\bdress\b(?!\s+shirt)", r"\bsundress\b", r"\bskirt\b"),
    ),
    CategoryRule(
        "hoodie",
        "Hoodie",
        (r"\bhoodie\b", r"\bhooded\b", r"\bzip hood\b", r"\bhood\b"),
    ),
    CategoryRule(
        "sweatshirt",
        "Sweatshirt",
        (
            r"\bsweatshirt\b",
            r"\bcrewneck\b",
            r"\bcrew neck fleece\b",
            r"\bsweater\b",
            r"\btie-dye fleece\b",
            r"\bfleece crew\b",
            r"\bheavy crew\b",
            r"\bmade crew\b",
            r"\brelax(?:ed)?(?: faded| crop)? crew\b",
            r"\bstencil(?: half zip)? crew\b",
            r"\bstretch jersey crew\b",
        ),
    ),
    CategoryRule(
        "outerwear",
        "Outerwear",
        (
            r"\bjacket\b",
            r"\bcoat\b",
            r"\bwindbreaker\b",
            r"\bbomber\b",
            r"\bparka\b",
            r"\bpuffer\b",
            r"\bfleece vest\b",
            r"\bsoft shell\b",
            r"\bsafety vest\b",
            r"\bwind shirt\b",
            r"\bpullover\b",
            r"\bvest\b",
            r"\bfull-zip\b",
            r"\b(?:1/2|1/4|quarter|half)[- ]zip\b",
            r"\brain shell\b",
            r"\banorak\b",
            r"\bcrop zip\b",
            r"\bouterwear\b",
            r"\bblazer\b",
        ),
    ),
    CategoryRule(
        "long-sleeve-shirt",
        "Long-sleeve shirt",
        (r"\blong[- ]sleeve\b", r"\braglan\b", r"\b3/4[- ]sleeve\b"),
    ),
    CategoryRule(
        "tank-top", "Tank top", (r"\btank(?: top)?\b", r"\bracerback\b", r"\bmuscle shirt\b")
    ),
    CategoryRule("polo-shirt", "Polo shirt", (r"\bpolo\b", r"\bhenley\b")),
    CategoryRule(
        "sportswear",
        "Sportswear",
        (
            r"\bsports? bra\b",
            r"\bbra top\b",
            r"\bluxe bra\b",
            r"\b(?:baseball|basketball|football|hockey|soccer|cycling) jersey\b",
            r"\bsports? jersey\b",
            r"\btwo-button jersey\b",
            r"\bfull-button jersey\b",
            r"\brugby jersey\b",
            r"\brash guard\b",
            r"\bcycling\b",
            r"\bperformance shirt\b",
            r"\bathletic shirt\b",
            r"\barm sleeve\b",
        ),
    ),
    CategoryRule(
        "bottoms",
        "Bottoms",
        (
            r"\bleggings?\b",
            r"\bjoggers?\b",
            r"\bsweatpants?\b",
            r"\bshorts\b",
            r"\bshort\b(?!\s+sleeve)",
            r"\bpants?\b",
            r"\btrousers?\b",
            r"\bskort\b",
        ),
    ),
    CategoryRule(
        "shirt",
        "Woven shirt",
        (
            r"\bbutton[- ](?:up|down)\b",
            r"\bdress shirt\b",
            r"\bhawaiian shirt\b",
            r"\bgingham.*shirt\b",
            r"\bstretch shirt\b",
            r"\bcheck shirt\b",
            r"\bjob shirt\b",
            r"\bcamp shirt\b",
            r"\bdenim shirt\b",
            r"\boxford shirt\b",
            r"\bflannel shirt\b",
            r"\bchambray shirt\b",
            r"\btwill shirt\b",
            r"\bwork (?:s-s )?shirt\b",
            r"\bsport shirt\b",
            r"\bblouse\b",
            r"\btunic\b",
            r"\bcardigan\b",
            r"\bmock turtleneck\b",
            r"\bpfg .*shirt\b",
            r"\bplaid pattern .*shirt\b",
            r"\bcrosshatch .*shirt\b",
            r"\bshort sleeve.*shirt\b",
        ),
    ),
    CategoryRule(
        "t-shirt",
        "T-shirt",
        (
            r"\bt[- ]?shirt\b",
            r"\btee\b",
            r"\bpresenter v-neck\b",
            r"\bcompetitor united crew\b",
            r"\bfaded shirt\b",
            r"\b(?:sleeveless |festival )?crop top\b",
            r"\bboxy top\b",
            r"\bdolman\b",
            r"\bv-neck coverup\b",
            r"\bopen neck top\b",
            r"\brelaxed scoop\b",
            r"\bultimate performance v-neck\b",
            r"\bclub sleeveless v-neck\b",
            r"\bstone wash heavy crop\b",
            r"\bshort sleeve(?: crew)?\b",
        ),
    ),
    CategoryRule(
        "water-bottle-tumbler",
        "Water bottle or tumbler",
        (
            r"\bwater bottle\b",
            r"\binsulated bottle\b",
            r"\bbottles?\b",
            r"\btumbler\b",
            r"\bvacuum (?:bottle|flask)\b",
            r"\btravel mug\b",
            r"\bcan cooler\b",
            r"\bstubby cooler\b",
            r"\bflask\b",
            r"\bprotein shaker\b",
            r"\binsulated cup\b",
        ),
    ),
    CategoryRule("mug", "Mug", (r"\bmugs?\b", r"\bcoffee cups?\b")),
    CategoryRule("candle", "Candle", (r"\bcandles?\b",)),
    CategoryRule(
        "glassware",
        "Glassware or cup",
        (
            r"\bglasses\b",
            r"\bglass(?:ware)?\b",
            r"\bwine glass\b",
            r"\bpint glass\b",
            r"\bshot glass\b",
            r"\bmason jar\b",
            r"\bcamp cup\b",
            r"\bsteel cup\b",
            r"\bacrylic cup\b",
        ),
    ),
    CategoryRule(
        "kitchen-accessory",
        "Kitchen accessory",
        (
            r"\bapron\b",
            r"\bcutting board\b",
            r"\bcoasters?\b",
            r"\bplacemat\b",
            r"\blunch box\b",
            r"\bkitchen towel\b",
            r"\btea towel\b",
            r"\boven mitt\b",
            r"\bnapkin\b",
            r"\b(?:pizza|charcuterie|serving) (?:board|tray)\b",
            r"\bbottle opener\b",
            r"\b(?:beverage|can) holder\b",
            r"\bice bucket\b",
            r"\bbento box\b",
            r"\bfood (?:jar|storage)\b",
            r"\btable runners?\b",
            r"\btablecloths?\b",
            r"\bkitchen dish mat\b",
            r"\bnapkins?\b",
            r"\boven mitts?\b",
        ),
    ),
    CategoryRule("ornament", "Ornament", (r"\bornaments?\b",)),
    CategoryRule(
        "party-supply",
        "Party supply",
        (r"\bballoons?\b",),
    ),
    CategoryRule(
        "seasonal-decoration",
        "Seasonal decoration",
        (
            r"\bstockings?\b",
            r"\btree skirts?\b",
            r"\bsnow globes?\b",
            r"\bholiday decorations?\b",
        ),
    ),
    CategoryRule("canvas-print", "Canvas print", (r"\bcanvas\b",)),
    CategoryRule(
        "poster",
        "Poster or wall print",
        (
            r"\bposters?\b",
            r"\bwall art\b",
            r"\bart prints?\b",
            r"\bacrylic prints?\b",
            r"\bunframed prints?\b",
        ),
    ),
    CategoryRule(
        "card",
        "Card or postcard",
        (
            r"\bpostcards?\b",
            r"\bgreeting cards?\b",
            r"\bnote cards?\b",
            r"\bholiday cards?\b",
        ),
    ),
    CategoryRule(
        "notebook",
        "Notebook or journal",
        (r"\bnotebook\b", r"\bjournal\b", r"\bcomposition book\b"),
    ),
    CategoryRule(
        "stationery",
        "Stationery accessory",
        (
            r"\bwrapping paper\b",
            r"\bcalendar\b",
            r"\bbookmark\b",
            r"\benvelope\b",
            r"\bpen\b",
            r"\bpencil\b",
            r"\bbusiness cards?\b",
            r"\bclipboard\b",
            r"\bgift wrap papers?\b",
            r"\bwrapping papers?\b",
            r"\bwall calendars?\b",
            r"\bnote cube\b",
            r"\bnote pads?\b",
        ),
    ),
    CategoryRule(
        "sticker-magnet",
        "Sticker, magnet, or pin",
        (
            r"\bstickers?\b",
            r"\bmagnets?\b",
            r"\bpin buttons?\b",
            r"\b(?:metal )?pins?\b",
            r"\bbadges?\b",
            r"\bpatch(?:es)?\b",
            r"\bvinyl decals?\b",
        ),
    ),
    CategoryRule("book", "Book", (r"\bbook\b", r"\bbible cover\b")),
    CategoryRule(
        "sports-game",
        "Sports or game accessory",
        (
            r"\bpuzzle\b",
            r"\bplaying cards\b",
            r"\byoga mat\b",
            r"\bgolf\b",
            r"\bpickleball\b",
            r"\bfrisbee\b",
            r"\bbaseball\b",
            r"\bbasketball\b",
            r"\bfootball\b",
            r"\bhockey (?:puck|.*stick)\b",
            r"\bping pong\b",
            r"\bpoker cards?\b",
            r"\bstadium seat\b",
        ),
    ),
    CategoryRule("blanket", "Blanket", (r"\bblankets?\b", r"\bthrow\b")),
    CategoryRule("pillow", "Pillow or cushion", (r"\bpillows?\b", r"\bcushion\b")),
    CategoryRule("towel", "Towel", (r"\btowels?\b",)),
    CategoryRule(
        "bathroom",
        "Bathroom accessory",
        (
            r"\bshower curtains?\b",
            r"\bbath mat\b",
            r"\bsoap dispenser\b",
            r"\btoothbrush holder\b",
            r"\bgrooming set\b",
        ),
    ),
    CategoryRule("rug-mat", "Rug or mat", (r"\brugs?\b", r"\bdoormats?\b", r"\bfloor mats?\b")),
    CategoryRule(
        "bedding",
        "Bedding",
        (
            r"\bduvet\b",
            r"\bcomforter\b",
            r"\bbed (?:set|sheet|runner)\b",
            r"\bfitted sheet\b",
            r"\bflat sheet\b",
            r"\bpillowcase\b",
            r"\bbedspread\b",
            r"\bquilt(?:ed)? (?:cover|coverlet|sham|bed runner)\b",
            r"\bwoven-style coverlet\b",
        ),
    ),
    CategoryRule(
        "home-decor",
        "Home decor",
        (
            r"\bclock\b",
            r"\blamp\b",
            r"\bplaque\b",
            r"\btapestry\b",
            r"\bwall decal\b",
            r"\bdecorative tray\b",
            r"\bphoto tile\b",
            r"\bsigns?\b",
            r"\bstandee\b",
            r"\bstatue\b",
            r"\bnight light\b",
            r"\btrinket trays?\b",
            r"\bflags?\b",
            r"\baluminum (?:composite )?panels?\b",
            r"\b(?:crystal )?award\b",
            r"\bfoam board\b",
            r"\bwooden decor\b",
            r"\bgallery board\b",
            r"\bbanners?\b",
            r"\bpennant\b",
            r"\bphoto block\b",
            r"\bwood panel painting\b",
            r"\bstorage box\b",
            r"\bwall decals?\b",
        ),
    ),
    CategoryRule(
        "window-treatment",
        "Window treatment",
        (r"\bwindow curtains?\b", r"\bsheer window curtain\b"),
    ),
    CategoryRule(
        "fabric-craft", "Fabric or craft supply", (r"\bfabric\b", r"\byarn\b", r"\bcraft\b")
    ),
    CategoryRule(
        "general-accessory",
        "General accessory",
        (
            r"\bkeychain\b",
            r"\bsunglasses\b",
            r"\bnecktie\b",
            r"\bscarf\b",
            r"\bumbrella\b",
            r"\blanyard\b",
            r"\bcard holder\b",
            r"\bcompact (?:square |travel )?mirror\b",
            r"\bkeyring\b",
            r"\bgraduation stole\b",
            r"\bscrunchie\b",
            r"\btemporary tattoos?\b",
        ),
    ),
)


PREFERRED_BLUEPRINTS = {
    "t-shirt": 706,  # Comfort Colors 1717
    "sweatshirt": 1296,  # Comfort Colors 1566
    "hoodie": 10953,  # AS Colour Heavy Hood
    "water-bottle-tumbler": 10661,  # BruMate Era 40 oz
    "mug": 635,  # Accent Coffee Mug
    "phone-case": 421,  # Tough Cases
    "laptop-tablet-sleeve": 5452,
    "mouse-pad": 582,
    "canvas-print": 944,
    "poster": 5651,  # Colored Frame Posters
    "notebook": 1931,
    "sticker-magnet": 400,
    "blanket": 1626,
    "pet-product": 1520,
    "bedding": 2706,  # Cotton Comforter
    "car-accessory": 893,  # Car Mats, set of four
    "face-mask": 970,  # Midweight Neck Gaiter
    "glassware": 1997,  # Engraved Whiskey Glass
    "pillow": 223,  # Faux Suede Square Pillow
    "sleepwear": 1037,  # Satin Pajamas
    "towel": 653,  # Mink-cotton towel
}

BRAND_SCORES = (
    ("brümate", 180),
    ("brumate", 180),
    ("stanley/stella", 170),
    ("as colour", 160),
    ("carhartt", 150),
    ("adidas", 145),
    ("columbia", 140),
    ("under armour", 140),
    ("champion", 130),
    ("comfort colors", 125),
    ("richardson", 115),
    ("yupoong", 110),
    ("independent trading", 105),
    ("bella+canvas", 100),
    ("bella + canvas", 100),
    ("lane seven", 95),
    ("next level", 90),
    ("gildan", 55),
    ("generic", -20),
)

QUALITY_TERMS = (
    ("organic", 65),
    ("heavyweight", 55),
    ("garment-dyed", 45),
    ("garment dyed", 45),
    ("premium", 40),
    ("insulated", 55),
    ("vacuum", 45),
    ("stainless steel", 45),
    ("copper", 35),
    ("leather", 45),
    ("tempered", 30),
    ("embroidered", 20),
    ("recycled", 20),
    ("cotton", 20),
    ("linen", 35),
    ("wool", 30),
    ("soy", 35),
    ("coconut apricot wax", 55),
    ("heavy duty", 40),
)


@dataclass(frozen=True)
class CuratedProduct:
    category: CategoryRule
    product: CatalogProduct
    score: int
    reasons: tuple[str, ...]

    @property
    def product_key(self) -> str:
        return f"{self.product.blueprint_id}:{self.product.print_provider_id}"


def classify_product(product: CatalogProduct) -> CategoryRule | None:
    preferred_category = next(
        (
            category
            for category, blueprint_id in PREFERRED_BLUEPRINTS.items()
            if blueprint_id == product.blueprint_id
        ),
        None,
    )
    if preferred_category is not None:
        return next(rule for rule in CATEGORY_RULES if rule.slug == preferred_category)
    searchable = " ".join(
        value for value in (product.title, product.brand, product.model) if value
    ).casefold()
    for rule in CATEGORY_RULES:
        if any(re.search(pattern, searchable, re.IGNORECASE) for pattern in rule.patterns):
            return rule
    return None


def _quality_score(product: CatalogProduct, category: CategoryRule) -> tuple[int, tuple[str, ...]]:
    score = 0
    reasons: list[str] = []
    searchable = " ".join(
        value for value in (product.title, product.brand, product.model) if value
    ).casefold()
    if PREFERRED_BLUEPRINTS.get(category.slug) == product.blueprint_id:
        score += 1500
        reasons.append("reviewed premium model for this category")
    if product.print_provider_id == PRINTIFY_CHOICE_PROVIDER_ID:
        score += 90
        reasons.append("available through Printify Choice")
    for brand, points in BRAND_SCORES:
        if brand in searchable:
            score += points
            if points > 0:
                reasons.append(f"premium brand signal: {brand}")
            break
    matched_terms: list[str] = []
    for term, points in QUALITY_TERMS:
        if term in searchable:
            score += points
            matched_terms.append(term)
    if matched_terms:
        reasons.append("quality construction: " + ", ".join(matched_terms))
    if any(tag.casefold() == "early access" for tag in product.tags):
        score -= 100
        reasons.append("early-access reliability penalty")
    available_variants = sum(variant.available for variant in product.variants)
    coverage_points = min(60, round(math.log2(available_variants + 1) * 8))
    score += coverage_points
    reasons.append(f"{available_variants} currently available variants")
    supported_surfaces = {
        (surface.position, surface.decoration_method)
        for variant in product.variants
        if variant.available
        for surface in variant.surfaces
        if surface.placement != "unsupported"
    }
    surface_points = min(20, len(supported_surfaces) * 4)
    score += surface_points
    if len(supported_surfaces) > 1:
        reasons.append(f"{len(supported_surfaces)} supported decoration surfaces")
    return score, tuple(reasons)


def select_best_products(products: list[CatalogProduct]) -> list[CuratedProduct]:
    selected: dict[str, CuratedProduct] = {}
    for product in products:
        category = classify_product(product)
        if category is None:
            continue
        score, reasons = _quality_score(product, category)
        candidate = CuratedProduct(category, product, score, reasons)
        incumbent = selected.get(category.slug)
        candidate_key = (
            candidate.score,
            sum(variant.available for variant in product.variants),
            product.print_provider_id == PRINTIFY_CHOICE_PROVIDER_ID,
            -product.blueprint_id,
            -product.print_provider_id,
        )
        if incumbent is None:
            selected[category.slug] = candidate
            continue
        incumbent_key = (
            incumbent.score,
            sum(variant.available for variant in incumbent.product.variants),
            incumbent.product.print_provider_id == PRINTIFY_CHOICE_PROVIDER_ID,
            -incumbent.product.blueprint_id,
            -incumbent.product.print_provider_id,
        )
        if candidate_key > incumbent_key:
            selected[category.slug] = candidate
    return sorted(selected.values(), key=lambda item: item.category.display_name)


def curate_synced_catalog(*, apply: bool = False) -> dict[str, Any]:
    with session_scope() as session:
        repository = CatalogRepository(session)
        products = repository.list_ranked_products()
        rank_by_blueprint = repository.rank_by_blueprint()
        if not products:
            if apply:
                repository.replace_curations([])
            return {
                "applied": apply,
                "algorithm_version": ALGORITHM_VERSION,
                "source_products": 0,
                "source_blueprints": 0,
                "classified_products": 0,
                "classified_blueprints": 0,
                "unclassified_blueprints": 0,
                "unclassified_title_sample": [],
                "categories": 0,
                "popular_categories": 0,
                "research_selections": [],
                "selections": [],
            }
        selections = select_best_products(products)
        payload: list[dict[str, Any]] = []
        for item in selections:
            rank = rank_by_blueprint[item.product.blueprint_id]
            payload.append(
                {
                    "category": item.category.slug,
                    "display_name": item.category.display_name,
                    "product_key": item.product_key,
                    "quality_score": item.score,
                    "reasons": list(item.reasons),
                    "research_priority": rank,
                    "popularity_reason": f"{POPULARITY_SOURCE}: rank {rank}",
                    "algorithm_version": ALGORITHM_VERSION,
                }
            )
        if apply:
            repository.replace_curations(payload)

        classified = [item for item in products if classify_product(item) is not None]
        classified_blueprints = {item.blueprint_id for item in classified}
        all_blueprints = {item.blueprint_id for item in products}
        unclassified_titles = sorted(
            {item.title for item in products if item.blueprint_id not in classified_blueprints}
        )
        return {
            "applied": apply,
            "algorithm_version": ALGORITHM_VERSION,
            "source_products": len(products),
            "source_blueprints": len(all_blueprints),
            "classified_products": len(classified),
            "classified_blueprints": len(classified_blueprints),
            "unclassified_blueprints": len(all_blueprints - classified_blueprints),
            "unclassified_title_sample": unclassified_titles[:100],
            "categories": len(selections),
            "popular_categories": len(payload),
            "research_selections": sorted(
                (
                    {
                        "category": row["category"],
                        "display_name": row["display_name"],
                        "product_key": row["product_key"],
                        "research_priority": row["research_priority"],
                        "popularity_reason": row["popularity_reason"],
                    }
                    for row in payload
                ),
                key=lambda row: (row["research_priority"], row["display_name"]),
            ),
            "selections": [
                {
                    **row,
                    "blueprint_id": item.product.blueprint_id,
                    "print_provider_id": item.product.print_provider_id,
                    "title": item.product.title,
                    "brand": item.product.brand,
                    "model": item.product.model,
                    "available_variants": sum(
                        variant.available for variant in item.product.variants
                    ),
                }
                for row, item in zip(payload, selections, strict=True)
            ],
        }
