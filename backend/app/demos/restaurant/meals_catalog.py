"""
Custom Meals — meals catalog + raw-materials inventory.

Two tables in one file because the Meals SPA page shows both side-by-
side and the data is tightly coupled (a meal references materials by
id; a material's `allergen_type` propagates up to every meal that uses
it).

Storage:
    data/demos/restaurant/meals_catalog.json

Tracked by git (operator-edited content); `.tmp` from atomic writes is
gitignored.

Allergen model
- Each raw material carries an optional `allergen_type` (e.g. "peanut",
  "dairy", "wheat", "shellfish"). Empty string means "no allergen".
- Each meal carries an explicit `allergens` list — the union of
  allergen-types over every material it references. The catalog
  recomputes this on every meal save, so the operator never has to
  type allergens manually; just pick the right materials.
- Downstream (subscriptions / schedules) reads `meal.allergens` and
  rejects any meal whose allergens intersect a client's allergies
  list, so the schedule picker can't poison anyone.
"""
from __future__ import annotations

import json
import logging
import threading
from copy import deepcopy
from pathlib import Path
from typing import Optional

logger = logging.getLogger("demo_restaurant.meals_catalog")

_DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "demos" / "restaurant"
_PATH    = _DATA_DIR / "meals_catalog.json"

_LOCK = threading.Lock()

DEFAULT_CURRENCY = "SAR"

# Allowed enumerations — kept short so the demo doesn't sprawl. The
# operator can still save custom values via the API; these drive the
# quick-pick chips on the SPA.
MEAL_CATEGORIES = ["breakfast", "lunch", "dinner", "snack"]
# "halal" intentionally omitted — every meal we serve is halal by
# default (Saudi context). Tagging each one would be noise; the
# Subscription plans no longer require it either, so the schedule
# filter stays internally consistent.
MEAL_DIET_TAGS  = ["vegetarian", "vegan", "gluten-free",
                   "dairy-free", "low-carb", "low-sodium",
                   "high-protein", "diabetic", "keto"]
ALLERGEN_TYPES  = ["peanut", "tree-nut", "dairy", "egg", "wheat",
                   "soy", "fish", "shellfish", "sesame", "sulphites"]
MATERIAL_UNITS  = ["g", "kg", "ml", "l", "pcs", "tbsp", "tsp"]


# ----------------------------------------------------------------------
# Raw materials seed — 30 ingredients with allergen mapping where it
# matters. Stock numbers are illustrative (the demo doesn't auto-
# deplete on schedule generation).
# ----------------------------------------------------------------------

DEFAULT_MATERIALS: list[dict] = [
    # Grains & starches
    {"id": "RAW-001", "name_en": "Basmati rice",     "name_ar": "أرز بسمتي",
     "unit": "kg", "current_stock": 80, "min_stock": 20, "cost_per_unit": 9.50,
     "supplier": "Al-Hassan Grains", "allergen_type": "", "active": True},
    {"id": "RAW-002", "name_en": "Bulgur",            "name_ar": "برغل",
     "unit": "kg", "current_stock": 35, "min_stock": 10, "cost_per_unit": 7.00,
     "supplier": "Al-Hassan Grains", "allergen_type": "wheat", "active": True},
    {"id": "RAW-003", "name_en": "Lentils (red)",     "name_ar": "عدس أحمر",
     "unit": "kg", "current_stock": 25, "min_stock":  8, "cost_per_unit": 11.00,
     "supplier": "Al-Hassan Grains", "allergen_type": "", "active": True},
    {"id": "RAW-004", "name_en": "Chickpeas (dry)",   "name_ar": "حمّص (يابس)",
     "unit": "kg", "current_stock": 40, "min_stock": 12, "cost_per_unit": 12.00,
     "supplier": "Al-Hassan Grains", "allergen_type": "", "active": True},
    {"id": "RAW-005", "name_en": "Whole-wheat flour", "name_ar": "طحين أسمر",
     "unit": "kg", "current_stock": 30, "min_stock": 10, "cost_per_unit": 6.50,
     "supplier": "Al-Hassan Grains", "allergen_type": "wheat", "active": True},
    {"id": "RAW-006", "name_en": "Oats (rolled)",     "name_ar": "شوفان",
     "unit": "kg", "current_stock": 15, "min_stock":  5, "cost_per_unit": 14.00,
     "supplier": "Al-Hassan Grains", "allergen_type": "", "active": True},

    # Proteins
    {"id": "RAW-010", "name_en": "Chicken breast",    "name_ar": "صدر دجاج",
     "unit": "kg", "current_stock": 35, "min_stock": 12, "cost_per_unit": 32.00,
     "supplier": "Riyadh Halal Meats", "allergen_type": "", "active": True},
    {"id": "RAW-011", "name_en": "Lamb (lean)",       "name_ar": "لحم غنم",
     "unit": "kg", "current_stock": 18, "min_stock":  6, "cost_per_unit": 78.00,
     "supplier": "Riyadh Halal Meats", "allergen_type": "", "active": True},
    {"id": "RAW-012", "name_en": "Beef (ground)",     "name_ar": "لحم بقر مفروم",
     "unit": "kg", "current_stock": 22, "min_stock":  8, "cost_per_unit": 58.00,
     "supplier": "Riyadh Halal Meats", "allergen_type": "", "active": True},
    {"id": "RAW-013", "name_en": "Salmon fillet",     "name_ar": "فيليه سلمون",
     "unit": "kg", "current_stock": 12, "min_stock":  4, "cost_per_unit": 95.00,
     "supplier": "Coastal Seafood",    "allergen_type": "fish", "active": True},
    {"id": "RAW-014", "name_en": "Shrimp",            "name_ar": "قريدس",
     "unit": "kg", "current_stock":  8, "min_stock":  3, "cost_per_unit": 78.00,
     "supplier": "Coastal Seafood",    "allergen_type": "shellfish", "active": True},
    {"id": "RAW-015", "name_en": "Eggs (large)",       "name_ar": "بيض",
     "unit": "pcs", "current_stock": 240, "min_stock": 60, "cost_per_unit": 0.55,
     "supplier": "Riyadh Farms",       "allergen_type": "egg", "active": True},
    {"id": "RAW-016", "name_en": "Firm tofu",          "name_ar": "توفو",
     "unit": "kg", "current_stock":  6, "min_stock":  2, "cost_per_unit": 28.00,
     "supplier": "Plant Pantry",       "allergen_type": "soy", "active": True},

    # Dairy
    {"id": "RAW-020", "name_en": "Greek yoghurt",       "name_ar": "زبادي يوناني",
     "unit": "kg", "current_stock": 14, "min_stock":  4, "cost_per_unit": 16.00,
     "supplier": "Almarai",            "allergen_type": "dairy", "active": True},
    {"id": "RAW-021", "name_en": "Labneh",              "name_ar": "لبنة",
     "unit": "kg", "current_stock": 10, "min_stock":  3, "cost_per_unit": 22.00,
     "supplier": "Almarai",            "allergen_type": "dairy", "active": True},
    {"id": "RAW-022", "name_en": "Feta cheese",         "name_ar": "جبنة فيتا",
     "unit": "kg", "current_stock":  6, "min_stock":  2, "cost_per_unit": 42.00,
     "supplier": "Almarai",            "allergen_type": "dairy", "active": True},
    {"id": "RAW-023", "name_en": "Halloumi",            "name_ar": "حلوم",
     "unit": "kg", "current_stock":  5, "min_stock":  2, "cost_per_unit": 55.00,
     "supplier": "Almarai",            "allergen_type": "dairy", "active": True},

    # Produce
    {"id": "RAW-030", "name_en": "Tomatoes",             "name_ar": "طماطم",
     "unit": "kg", "current_stock": 28, "min_stock":  8, "cost_per_unit": 5.50,
     "supplier": "Local Produce",      "allergen_type": "", "active": True},
    {"id": "RAW-031", "name_en": "Cucumber",             "name_ar": "خيار",
     "unit": "kg", "current_stock": 22, "min_stock":  6, "cost_per_unit": 4.20,
     "supplier": "Local Produce",      "allergen_type": "", "active": True},
    {"id": "RAW-032", "name_en": "Yellow onion",          "name_ar": "بصل",
     "unit": "kg", "current_stock": 30, "min_stock": 10, "cost_per_unit": 3.20,
     "supplier": "Local Produce",      "allergen_type": "", "active": True},
    {"id": "RAW-033", "name_en": "Parsley (flat-leaf)",   "name_ar": "بقدونس",
     "unit": "kg", "current_stock":  4, "min_stock":  1, "cost_per_unit": 12.00,
     "supplier": "Local Produce",      "allergen_type": "", "active": True},
    {"id": "RAW-034", "name_en": "Mint",                   "name_ar": "نعناع",
     "unit": "kg", "current_stock":  3, "min_stock":  1, "cost_per_unit": 14.00,
     "supplier": "Local Produce",      "allergen_type": "", "active": True},
    {"id": "RAW-035", "name_en": "Lemons",                "name_ar": "ليمون",
     "unit": "kg", "current_stock": 18, "min_stock":  5, "cost_per_unit": 6.80,
     "supplier": "Local Produce",      "allergen_type": "", "active": True},
    {"id": "RAW-036", "name_en": "Garlic",                "name_ar": "ثوم",
     "unit": "kg", "current_stock":  4, "min_stock":  1, "cost_per_unit": 22.00,
     "supplier": "Local Produce",      "allergen_type": "", "active": True},
    {"id": "RAW-037", "name_en": "Spinach",                "name_ar": "سبانخ",
     "unit": "kg", "current_stock":  7, "min_stock":  2, "cost_per_unit": 9.50,
     "supplier": "Local Produce",      "allergen_type": "", "active": True},
    {"id": "RAW-038", "name_en": "Romaine lettuce",        "name_ar": "خس",
     "unit": "kg", "current_stock":  9, "min_stock":  3, "cost_per_unit": 7.00,
     "supplier": "Local Produce",      "allergen_type": "", "active": True},

    # Pantry / oils / nuts / seeds
    {"id": "RAW-040", "name_en": "Olive oil (extra virgin)", "name_ar": "زيت زيتون بكر",
     "unit": "l",  "current_stock": 24, "min_stock":  6, "cost_per_unit": 38.00,
     "supplier": "Mount Lebanon",      "allergen_type": "", "active": True},
    {"id": "RAW-041", "name_en": "Tahini",                   "name_ar": "طحينة",
     "unit": "kg", "current_stock":  6, "min_stock":  2, "cost_per_unit": 32.00,
     "supplier": "Mount Lebanon",      "allergen_type": "sesame", "active": True},
    {"id": "RAW-042", "name_en": "Pine nuts",                "name_ar": "صنوبر",
     "unit": "kg", "current_stock":  2, "min_stock":  1, "cost_per_unit": 320.00,
     "supplier": "Mount Lebanon",      "allergen_type": "tree-nut", "active": True},
    {"id": "RAW-043", "name_en": "Walnuts",                  "name_ar": "جوز",
     "unit": "kg", "current_stock":  3, "min_stock":  1, "cost_per_unit": 95.00,
     "supplier": "Mount Lebanon",      "allergen_type": "tree-nut", "active": True},
    {"id": "RAW-044", "name_en": "Sesame seeds",             "name_ar": "سمسم",
     "unit": "kg", "current_stock":  4, "min_stock":  1, "cost_per_unit": 28.00,
     "supplier": "Mount Lebanon",      "allergen_type": "sesame", "active": True},
    {"id": "RAW-045", "name_en": "Zaatar mix",                "name_ar": "زعتر",
     "unit": "kg", "current_stock":  3, "min_stock":  1, "cost_per_unit": 38.00,
     "supplier": "Mount Lebanon",      "allergen_type": "sesame", "active": True},
    {"id": "RAW-046", "name_en": "Sumac",                     "name_ar": "سماق",
     "unit": "kg", "current_stock":  2, "min_stock":  1, "cost_per_unit": 60.00,
     "supplier": "Spice House",        "allergen_type": "", "active": True},
    {"id": "RAW-047", "name_en": "Cumin (ground)",            "name_ar": "كمون",
     "unit": "kg", "current_stock":  2, "min_stock":  1, "cost_per_unit": 48.00,
     "supplier": "Spice House",        "allergen_type": "", "active": True},
]


# ----------------------------------------------------------------------
# Meals seed — 24 meals across breakfast / lunch / dinner / snack.
# `material_ids` references RAW-* above. Allergens are AUTO-computed
# from materials on every save; the values stored here are correct for
# the seed but the system never trusts them at write time.
# ----------------------------------------------------------------------

DEFAULT_MEALS: list[dict] = [
    # ----------------- BREAKFAST -----------------
    {"id": "MEAL-001", "category": "breakfast",
     "name_en": "Zaatar Manakish",            "name_ar": "مناقيش زعتر",
     "description_en": "Flatbread baked with zaatar and olive oil.",
     "description_ar": "خبز مرقوق محمّر بالزعتر وزيت الزيتون.",
     "portion_size_g": 220, "prep_time_min": 15,
     "calories": 380, "protein_g": 10, "carbs_g": 52, "fat_g": 14, "fiber_g": 4,
     "diet_tags": ["vegetarian"],
     "material_ids": ["RAW-005", "RAW-040", "RAW-045"],
     "allergens": ["wheat", "sesame"],
     "price": 22.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-002", "category": "breakfast",
     "name_en": "Labneh Bowl",                 "name_ar": "صحن لبنة",
     "description_en": "Labneh topped with olive oil, mint, cucumber.",
     "description_ar": "لبنة مع زيت زيتون ونعناع وخيار.",
     "portion_size_g": 280, "prep_time_min": 7,
     "calories": 320, "protein_g": 16, "carbs_g": 14, "fat_g": 22, "fiber_g": 2,
     "diet_tags": ["vegetarian", "low-carb"],
     "material_ids": ["RAW-021", "RAW-040", "RAW-031", "RAW-034"],
     "allergens": ["dairy"],
     "price": 24.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-003", "category": "breakfast",
     "name_en": "Foul Medames",                "name_ar": "فول مدمس",
     "description_en": "Slow-cooked fava beans with cumin and lemon.",
     "description_ar": "فول مدمس بالكمون والليمون.",
     "portion_size_g": 320, "prep_time_min": 20,
     "calories": 360, "protein_g": 18, "carbs_g": 48, "fat_g": 10, "fiber_g": 14,
     "diet_tags": ["vegan", "vegetarian", "high-protein"],
     "material_ids": ["RAW-004", "RAW-035", "RAW-036", "RAW-040", "RAW-047"],
     "allergens": [],
     "price": 20.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-004", "category": "breakfast",
     "name_en": "Veggie Shakshuka",            "name_ar": "شكشوكة خضار",
     "description_en": "Eggs poached in spiced tomato sauce.",
     "description_ar": "بيض مطبوخ بصلصة طماطم متبّلة.",
     "portion_size_g": 320, "prep_time_min": 22,
     "calories": 410, "protein_g": 22, "carbs_g": 26, "fat_g": 22, "fiber_g": 6,
     "diet_tags": ["vegetarian", "low-carb"],
     "material_ids": ["RAW-015", "RAW-030", "RAW-032", "RAW-036", "RAW-040", "RAW-047"],
     "allergens": ["egg"],
     "price": 28.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-005", "category": "breakfast",
     "name_en": "Overnight Oats",               "name_ar": "شوفان منقوع",
     "description_en": "Rolled oats soaked with yoghurt, mint, lemon zest.",
     "description_ar": "شوفان منقوع بالزبادي والنعناع.",
     "portion_size_g": 280, "prep_time_min": 5,
     "calories": 360, "protein_g": 18, "carbs_g": 50, "fat_g": 8, "fiber_g": 8,
     "diet_tags": ["vegetarian", "high-protein"],
     "material_ids": ["RAW-006", "RAW-020", "RAW-034", "RAW-035"],
     "allergens": ["dairy"],
     "price": 22.00, "currency": DEFAULT_CURRENCY, "active": True},

    # ----------------- LUNCH -----------------
    {"id": "MEAL-010", "category": "lunch",
     "name_en": "Grilled Chicken Tabbouleh Bowl", "name_ar": "صحن دجاج وتبّولة",
     "description_en": "Grilled chicken breast over tabbouleh with olive-lemon dressing.",
     "description_ar": "دجاج مشوي مع تبّولة وصلصة ليمون.",
     "portion_size_g": 420, "prep_time_min": 25,
     "calories": 520, "protein_g": 42, "carbs_g": 38, "fat_g": 18, "fiber_g": 8,
     "diet_tags": ["high-protein"],
     "material_ids": ["RAW-010", "RAW-002", "RAW-030", "RAW-033", "RAW-034",
                       "RAW-035", "RAW-040"],
     "allergens": ["wheat"],
     "price": 48.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-011", "category": "lunch",
     "name_en": "Lentil Mujadara",                "name_ar": "مجدّرة",
     "description_en": "Lentils and rice with caramelised onion.",
     "description_ar": "عدس وأرز مع بصل محمّر.",
     "portion_size_g": 380, "prep_time_min": 30,
     "calories": 480, "protein_g": 18, "carbs_g": 78, "fat_g": 10, "fiber_g": 12,
     "diet_tags": ["vegan", "vegetarian", "high-protein"],
     "material_ids": ["RAW-003", "RAW-001", "RAW-032", "RAW-040", "RAW-047"],
     "allergens": [],
     "price": 32.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-012", "category": "lunch",
     "name_en": "Salmon & Quinoa Plate",          "name_ar": "سلمون وكينوا",
     "description_en": "Oven-baked salmon with lemon herbs and quinoa.",
     "description_ar": "سلمون مشوي بالأعشاب مع كينوا.",
     "portion_size_g": 380, "prep_time_min": 28,
     "calories": 540, "protein_g": 40, "carbs_g": 36, "fat_g": 24, "fiber_g": 6,
     "diet_tags": ["high-protein", "low-carb"],
     "material_ids": ["RAW-013", "RAW-035", "RAW-033", "RAW-040"],
     "allergens": ["fish"],
     "price": 78.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-013", "category": "lunch",
     "name_en": "Vegetable Falafel Bowl",          "name_ar": "صحن فلافل خضار",
     "description_en": "Baked falafel with tahini sauce, salad and hummus.",
     "description_ar": "فلافل مع طحينة وسلطة وحمّص.",
     "portion_size_g": 420, "prep_time_min": 25,
     "calories": 540, "protein_g": 22, "carbs_g": 60, "fat_g": 24, "fiber_g": 14,
     "diet_tags": ["vegan", "vegetarian", "high-protein"],
     "material_ids": ["RAW-004", "RAW-041", "RAW-033", "RAW-031", "RAW-030",
                       "RAW-040", "RAW-047"],
     "allergens": ["sesame"],
     "price": 38.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-014", "category": "lunch",
     "name_en": "Lamb Kabsa (lean)",                "name_ar": "كبسة لحم خفيف",
     "description_en": "Saudi-style lamb and spiced rice; trimmed lamb.",
     "description_ar": "كبسة لحم بأرز بهارات (خفيفة).",
     "portion_size_g": 440, "prep_time_min": 50,
     "calories": 620, "protein_g": 38, "carbs_g": 62, "fat_g": 22, "fiber_g": 4,
     "diet_tags": ["high-protein"],
     "material_ids": ["RAW-011", "RAW-001", "RAW-032", "RAW-030", "RAW-040", "RAW-047"],
     "allergens": [],
     "price": 65.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-015", "category": "lunch",
     "name_en": "Halloumi & Greens Salad",          "name_ar": "سلطة حلوم وخضار",
     "description_en": "Grilled halloumi, romaine, tomato, sumac dressing.",
     "description_ar": "حلوم مشوي مع خس وطماطم وصلصة السماق.",
     "portion_size_g": 360, "prep_time_min": 18,
     "calories": 480, "protein_g": 26, "carbs_g": 18, "fat_g": 32, "fiber_g": 6,
     "diet_tags": ["vegetarian", "low-carb", "keto"],
     "material_ids": ["RAW-023", "RAW-038", "RAW-030", "RAW-040", "RAW-046"],
     "allergens": ["dairy"],
     "price": 42.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-016", "category": "lunch",
     "name_en": "Shrimp Stir-fry",                  "name_ar": "قريدس مقلي",
     "description_en": "Shrimp sautéed with garlic, lemon and herbs.",
     "description_ar": "قريدس مقلي بالثوم والليمون.",
     "portion_size_g": 360, "prep_time_min": 20,
     "calories": 460, "protein_g": 34, "carbs_g": 22, "fat_g": 22, "fiber_g": 5,
     "diet_tags": ["high-protein", "low-carb"],
     "material_ids": ["RAW-014", "RAW-036", "RAW-035", "RAW-040", "RAW-037"],
     "allergens": ["shellfish"],
     "price": 72.00, "currency": DEFAULT_CURRENCY, "active": True},

    # ----------------- DINNER -----------------
    {"id": "MEAL-020", "category": "dinner",
     "name_en": "Tofu Stew",                         "name_ar": "يخنة توفو",
     "description_en": "Firm tofu simmered with vegetables and herbs.",
     "description_ar": "توفو مع خضار وأعشاب.",
     "portion_size_g": 380, "prep_time_min": 32,
     "calories": 380, "protein_g": 28, "carbs_g": 24, "fat_g": 18, "fiber_g": 8,
     "diet_tags": ["vegan", "vegetarian", "high-protein", "low-carb"],
     "material_ids": ["RAW-016", "RAW-030", "RAW-032", "RAW-036", "RAW-040"],
     "allergens": ["soy"],
     "price": 42.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-021", "category": "dinner",
     "name_en": "Beef Kafta with Salad",             "name_ar": "كفتة لحم مع سلطة",
     "description_en": "Grilled beef kafta over romaine with tomato salad.",
     "description_ar": "كفتة لحم مع سلطة طازجة.",
     "portion_size_g": 400, "prep_time_min": 25,
     "calories": 560, "protein_g": 38, "carbs_g": 18, "fat_g": 36, "fiber_g": 6,
     "diet_tags": ["high-protein", "low-carb"],
     "material_ids": ["RAW-012", "RAW-038", "RAW-030", "RAW-033", "RAW-040"],
     "allergens": [],
     "price": 55.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-022", "category": "dinner",
     "name_en": "Spinach & Lentil Soup",             "name_ar": "شوربة عدس وسبانخ",
     "description_en": "Red lentils, spinach, cumin — comforting and high-fiber.",
     "description_ar": "شوربة عدس أحمر مع سبانخ.",
     "portion_size_g": 380, "prep_time_min": 25,
     "calories": 320, "protein_g": 18, "carbs_g": 44, "fat_g": 8, "fiber_g": 12,
     "diet_tags": ["vegan", "vegetarian", "high-protein", "low-sodium"],
     "material_ids": ["RAW-003", "RAW-037", "RAW-032", "RAW-035", "RAW-047"],
     "allergens": [],
     "price": 28.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-023", "category": "dinner",
     "name_en": "Baked Chicken & Vegetables",        "name_ar": "دجاج بالفرن مع خضار",
     "description_en": "Boneless chicken with roasted veg and lemon.",
     "description_ar": "دجاج بالفرن مع خضار وليمون.",
     "portion_size_g": 420, "prep_time_min": 35,
     "calories": 480, "protein_g": 44, "carbs_g": 28, "fat_g": 18, "fiber_g": 7,
     "diet_tags": ["high-protein", "low-carb"],
     "material_ids": ["RAW-010", "RAW-030", "RAW-032", "RAW-035", "RAW-037", "RAW-040"],
     "allergens": [],
     "price": 52.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-024", "category": "dinner",
     "name_en": "Salmon & Greens",                    "name_ar": "سلمون مع خضار",
     "description_en": "Pan-seared salmon over wilted spinach.",
     "description_ar": "سلمون مع سبانخ مطبوخة.",
     "portion_size_g": 360, "prep_time_min": 22,
     "calories": 490, "protein_g": 38, "carbs_g": 10, "fat_g": 30, "fiber_g": 4,
     "diet_tags": ["high-protein", "low-carb", "keto"],
     "material_ids": ["RAW-013", "RAW-037", "RAW-036", "RAW-035", "RAW-040"],
     "allergens": ["fish"],
     "price": 78.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-025", "category": "dinner",
     "name_en": "Stuffed Cabbage Rolls (vegan)",      "name_ar": "ملفوف نباتي",
     "description_en": "Cabbage rolls stuffed with rice, lentils and herbs.",
     "description_ar": "ملفوف محشي بالأرز والعدس والأعشاب.",
     "portion_size_g": 400, "prep_time_min": 45,
     "calories": 420, "protein_g": 14, "carbs_g": 68, "fat_g": 10, "fiber_g": 10,
     "diet_tags": ["vegan", "vegetarian"],
     "material_ids": ["RAW-001", "RAW-003", "RAW-032", "RAW-033", "RAW-035", "RAW-040"],
     "allergens": [],
     "price": 38.00, "currency": DEFAULT_CURRENCY, "active": True},

    # ----------------- SNACK -----------------
    {"id": "MEAL-030", "category": "snack",
     "name_en": "Hummus & Cucumber",                  "name_ar": "حمّص مع خيار",
     "description_en": "Classic hummus with sliced cucumber sticks.",
     "description_ar": "حمّص مع شرائح خيار.",
     "portion_size_g": 180, "prep_time_min": 6,
     "calories": 240, "protein_g": 9, "carbs_g": 28, "fat_g": 11, "fiber_g": 7,
     "diet_tags": ["vegan", "vegetarian"],
     "material_ids": ["RAW-004", "RAW-041", "RAW-031", "RAW-035", "RAW-040"],
     "allergens": ["sesame"],
     "price": 16.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-031", "category": "snack",
     "name_en": "Greek Yoghurt + Walnuts",            "name_ar": "زبادي مع جوز",
     "description_en": "Greek yoghurt topped with crushed walnuts.",
     "description_ar": "زبادي يوناني مع قطع الجوز.",
     "portion_size_g": 220, "prep_time_min": 3,
     "calories": 280, "protein_g": 18, "carbs_g": 14, "fat_g": 16, "fiber_g": 2,
     "diet_tags": ["vegetarian", "high-protein"],
     "material_ids": ["RAW-020", "RAW-043"],
     "allergens": ["dairy", "tree-nut"],
     "price": 18.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-032", "category": "snack",
     "name_en": "Boiled Eggs (3)",                     "name_ar": "بيض مسلوق (3)",
     "description_en": "Three hard-boiled eggs with sumac and salt.",
     "description_ar": "ثلاث بيضات مسلوقة مع سماق وملح.",
     "portion_size_g": 160, "prep_time_min": 12,
     "calories": 230, "protein_g": 19, "carbs_g": 2, "fat_g": 16, "fiber_g": 0,
     "diet_tags": ["vegetarian", "high-protein", "low-carb", "keto"],
     "material_ids": ["RAW-015", "RAW-046"],
     "allergens": ["egg"],
     "price": 12.00, "currency": DEFAULT_CURRENCY, "active": True},
    {"id": "MEAL-033", "category": "snack",
     "name_en": "Tabbouleh Cup",                       "name_ar": "كوب تبّولة",
     "description_en": "Single-serve tabbouleh — high-fiber side.",
     "description_ar": "تبّولة بحجم فردي.",
     "portion_size_g": 180, "prep_time_min": 12,
     "calories": 180, "protein_g": 5, "carbs_g": 24, "fat_g": 7, "fiber_g": 5,
     "diet_tags": ["vegan", "vegetarian"],
     "material_ids": ["RAW-002", "RAW-033", "RAW-034", "RAW-030", "RAW-035", "RAW-040"],
     "allergens": ["wheat"],
     "price": 14.00, "currency": DEFAULT_CURRENCY, "active": True},
]


def _default_payload() -> dict:
    return {
        "meals":         deepcopy(DEFAULT_MEALS),
        "raw_materials": deepcopy(DEFAULT_MATERIALS),
    }


# ----------------------------------------------------------------------
# Load / save / reset
# ----------------------------------------------------------------------

def load_catalog() -> dict:
    """Return both lists. Falls back to seed when missing/corrupt."""
    with _LOCK:
        data = _load_locked()
    # Recompute meal allergens from current materials on every read, so
    # if the operator changed a material's allergen_type the meals
    # automatically reflect it.
    mats_by_id = {m["id"]: m for m in data["raw_materials"]}
    for meal in data["meals"]:
        meal["allergens"] = _allergens_from_materials(meal.get("material_ids") or [], mats_by_id)
    return data


def _load_locked() -> dict:
    if not _PATH.exists():
        return _default_payload()
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("meals_catalog.json corrupt — falling back to seed")
        return _default_payload()
    if not isinstance(d, dict):
        return _default_payload()
    meals = d.get("meals")
    mats  = d.get("raw_materials")
    return {
        "meals":         [m for m in (meals or []) if isinstance(m, dict)] or deepcopy(DEFAULT_MEALS),
        "raw_materials": [m for m in (mats or [])  if isinstance(m, dict)] or deepcopy(DEFAULT_MATERIALS),
    }


def _save_locked(payload: dict) -> dict:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(_PATH)
    return payload


def reset_catalog() -> dict:
    with _LOCK:
        _save_locked(_default_payload())
    return load_catalog()


# ----------------------------------------------------------------------
# Allergen derivation
# ----------------------------------------------------------------------

def _allergens_from_materials(material_ids: list[str], mats_by_id: dict) -> list[str]:
    """Union of allergen-types over the referenced materials. Stable
    order so the JSON output is diff-friendly."""
    out: list[str] = []
    seen: set[str] = set()
    for mid in material_ids:
        m = mats_by_id.get(mid)
        if not m:
            continue
        a = (m.get("allergen_type") or "").strip().lower()
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return sorted(out)


# ----------------------------------------------------------------------
# Meal CRUD
# ----------------------------------------------------------------------

def _next_id(rows: list[dict], prefix: str) -> str:
    n = 0
    for r in rows:
        rid = str((r or {}).get("id") or "")
        if rid.startswith(prefix + "-"):
            try: n = max(n, int(rid[len(prefix) + 1:]))
            except Exception: pass
    return f"{prefix}-{n + 1:03d}"


class _Refused(ValueError):
    """Raised when the request is rejected at the validation layer."""


def _coerce_meal(m: dict, existing_meals: list[dict],
                  mats_by_id: dict) -> dict:
    cat = str(m.get("category") or "lunch").strip().lower()
    if cat not in MEAL_CATEGORIES:
        cat = "lunch"
    tags = m.get("diet_tags") or []
    if not isinstance(tags, list): tags = []
    mids = m.get("material_ids") or []
    if not isinstance(mids, list): mids = []
    return {
        "id":              str(m.get("id") or _next_id(existing_meals, "MEAL")).strip(),
        "category":        cat,
        "name_en":         str(m.get("name_en") or "").strip()[:200],
        "name_ar":         str(m.get("name_ar") or "").strip()[:200],
        "description_en":  str(m.get("description_en") or "").strip()[:600],
        "description_ar":  str(m.get("description_ar") or "").strip()[:600],
        "portion_size_g":  max(0, int(m.get("portion_size_g") or 0)),
        "prep_time_min":   max(0, int(m.get("prep_time_min") or 0)),
        "calories":        max(0, int(m.get("calories") or 0)),
        "protein_g":       max(0, int(m.get("protein_g") or 0)),
        "carbs_g":         max(0, int(m.get("carbs_g") or 0)),
        "fat_g":           max(0, int(m.get("fat_g") or 0)),
        "fiber_g":         max(0, int(m.get("fiber_g") or 0)),
        "diet_tags":       sorted({str(t).strip().lower()[:32] for t in tags if str(t).strip()}),
        "material_ids":   [str(x).strip() for x in mids if str(x).strip()],
        "allergens":       _allergens_from_materials(mids, mats_by_id),
        "price":           round(max(0.0, float(m.get("price") or 0)), 2),
        "currency":        (str(m.get("currency") or DEFAULT_CURRENCY).strip()
                             or DEFAULT_CURRENCY)[:8],
        "active":          bool(m.get("active", True)),
    }


def add_meal(patch: dict) -> dict:
    if not (patch.get("name_en") or patch.get("name_ar") or "").strip():
        raise _Refused("Meal needs at least one name (EN or AR).")
    with _LOCK:
        payload = _load_locked()
        mats_by_id = {m["id"]: m for m in payload["raw_materials"]}
        meal = _coerce_meal(patch, payload["meals"], mats_by_id)
        payload["meals"].append(meal)
        _save_locked(payload)
        return meal


def update_meal(meal_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        payload = _load_locked()
        mats_by_id = {m["id"]: m for m in payload["raw_materials"]}
        for i, m in enumerate(payload["meals"]):
            if m.get("id") == meal_id:
                merged = {**m, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = meal_id
                if not (merged.get("name_en") or merged.get("name_ar") or "").strip():
                    raise _Refused("Meal needs at least one name (EN or AR).")
                payload["meals"][i] = _coerce_meal(merged, payload["meals"], mats_by_id)
                _save_locked(payload)
                return payload["meals"][i]
    return None


def delete_meal(meal_id: str) -> bool:
    with _LOCK:
        payload = _load_locked()
        before = len(payload["meals"])
        payload["meals"] = [m for m in payload["meals"] if m.get("id") != meal_id]
        removed = len(payload["meals"]) < before
        if removed:
            _save_locked(payload)
        return removed


# ----------------------------------------------------------------------
# Raw-material CRUD
# ----------------------------------------------------------------------

def _coerce_material(m: dict, existing: list[dict]) -> dict:
    unit = str(m.get("unit") or "kg").strip().lower()
    if unit not in MATERIAL_UNITS:
        unit = "kg"
    allergen = str(m.get("allergen_type") or "").strip().lower()
    if allergen and allergen not in ALLERGEN_TYPES:
        # Allow custom allergens but warn — operator might be in error.
        logger.info("Custom allergen_type stored: %s", allergen)
    return {
        "id":              str(m.get("id") or _next_id(existing, "RAW")).strip(),
        "name_en":         str(m.get("name_en") or "").strip()[:200],
        "name_ar":         str(m.get("name_ar") or "").strip()[:200],
        "unit":            unit,
        "current_stock":   round(max(0.0, float(m.get("current_stock") or 0)), 2),
        "min_stock":       round(max(0.0, float(m.get("min_stock") or 0)), 2),
        "cost_per_unit":   round(max(0.0, float(m.get("cost_per_unit") or 0)), 2),
        "supplier":        str(m.get("supplier") or "").strip()[:200],
        "allergen_type":   allergen,
        "active":          bool(m.get("active", True)),
    }


def add_material(patch: dict) -> dict:
    if not (patch.get("name_en") or patch.get("name_ar") or "").strip():
        raise _Refused("Material needs at least one name (EN or AR).")
    with _LOCK:
        payload = _load_locked()
        mat = _coerce_material(patch, payload["raw_materials"])
        payload["raw_materials"].append(mat)
        _save_locked(payload)
        return mat


def update_material(mat_id: str, patch: dict) -> Optional[dict]:
    with _LOCK:
        payload = _load_locked()
        for i, m in enumerate(payload["raw_materials"]):
            if m.get("id") == mat_id:
                merged = {**m, **{k: v for k, v in patch.items() if v is not None}}
                merged["id"] = mat_id
                if not (merged.get("name_en") or merged.get("name_ar") or "").strip():
                    raise _Refused("Material needs at least one name (EN or AR).")
                payload["raw_materials"][i] = _coerce_material(merged, payload["raw_materials"])
                _save_locked(payload)
                return payload["raw_materials"][i]
    return None


def delete_material(mat_id: str) -> dict:
    """Delete a material. Returns counts of meals that referenced it
    (they keep the orphaned reference; the operator sees a warning chip
    on the meal row). We don't cascade-delete meals."""
    with _LOCK:
        payload = _load_locked()
        before = len(payload["raw_materials"])
        payload["raw_materials"] = [m for m in payload["raw_materials"] if m.get("id") != mat_id]
        if len(payload["raw_materials"]) == before:
            return {"removed": False, "meals_referencing": 0}
        # Count meals that still reference this material so the
        # operator can decide what to do about them.
        n_refs = sum(
            1 for meal in payload["meals"]
            if mat_id in (meal.get("material_ids") or [])
        )
        _save_locked(payload)
        return {"removed": True, "meals_referencing": n_refs}


# ----------------------------------------------------------------------
# Read helpers (used by subscriptions / schedules modules)
# ----------------------------------------------------------------------

def meals_suitable_for(client_allergies: list[str],
                        diet_tags_required: list[str],
                        category: Optional[str] = None) -> list[dict]:
    """Return every active meal whose allergens don't intersect the
    client's allergies AND which has every diet-tag the plan requires
    (intersection-subset). Optionally narrow to a meal category.
    This is the canonical filter used everywhere a "what can I serve
    this client" decision is made."""
    catalog = load_catalog()
    forbid = {a.strip().lower() for a in client_allergies if a and a.strip()}
    need   = {t.strip().lower() for t in diet_tags_required if t and t.strip()}
    out: list[dict] = []
    for m in catalog["meals"]:
        if not m.get("active", True):
            continue
        if category and (m.get("category") or "") != category:
            continue
        if forbid & set(m.get("allergens") or []):
            continue
        if not need.issubset(set(m.get("diet_tags") or [])):
            continue
        out.append(m)
    return out
