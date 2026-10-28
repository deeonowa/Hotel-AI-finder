"""Закрытые словари (enum'ы) — общий язык NLU, данных, ранжирования и UI.

Правило: новое значение добавляется ТОЛЬКО через PR, который видят все трое.
Почему закрытые словари, а не свободный текст: LLM выдаёт значения из фиксированного
набора → ранжирование детерминировано, промпт-инъекции не могут «придумать» новое поле,
фронт знает все варианты заранее (типы генерируются из OpenAPI).
"""

from enum import StrEnum


class Source(StrEnum):
    """Откуда взято значение требования. Показывается пользователю чипсами."""

    user = "user"  # пользователь сказал явно
    assumed = "assumed"  # агент предположил (обязательно видно в UI и редактируется)
    profile = "profile"  # из памяти о прошлых поездках
    default = "default"  # системное значение по умолчанию


class Priority(StrEnum):
    must = "must"  # жёсткое ограничение: нарушать нельзя, вариант отсекается
    important = "important"  # сильное предпочтение: большой вес в скоре
    nice = "nice"  # приятно, если будет


class PrefKey(StrEnum):
    """Что может быть важно путешественнику. Каждый ключ обязан иметь обработчик в ranking/."""

    near_sea = "near_sea"  # value: макс. метров до моря (опц.)
    beach_first_line = "beach_first_line"
    sea_view = "sea_view"
    mountains = "mountains"
    ski_access = "ski_access"
    quiet = "quiet"
    nightlife = "nightlife"
    city_center = "city_center"
    family_friendly = "family_friendly"
    kids_club = "kids_club"
    pool = "pool"
    heated_pool = "heated_pool"
    spa = "spa"
    romantic = "romantic"
    breakfast_included = "breakfast_included"
    all_inclusive = "all_inclusive"
    free_cancellation = "free_cancellation"
    pet_friendly = "pet_friendly"
    parking = "parking"
    accessible = "accessible"  # доступная среда
    short_transfer = "short_transfer"  # value: макс. минут трансфера (опц.)
    high_rating = "high_rating"  # value: мин. рейтинг 0..10 (опц.)
    min_stars = "min_stars"  # value: мин. звёзд
    budget_value = "budget_value"  # «подешевле», цена важнее всего
    luxury = "luxury"
    work_friendly = "work_friendly"
    nature = "nature"
    wellness = "wellness"  # санаторий, лечение, процедуры


class Amenity(StrEnum):
    pool = "pool"
    heated_pool = "heated_pool"
    indoor_pool = "indoor_pool"
    spa = "spa"
    sauna = "sauna"
    gym = "gym"
    kids_club = "kids_club"
    playground = "playground"
    kids_menu = "kids_menu"
    private_beach = "private_beach"
    beach_shuttle = "beach_shuttle"
    restaurant = "restaurant"
    bar = "bar"
    parking = "parking"
    wifi = "wifi"
    workspace = "workspace"
    elevator = "elevator"
    wheelchair_accessible = "wheelchair_accessible"
    airport_shuttle = "airport_shuttle"
    ski_storage = "ski_storage"
    ski_in_out = "ski_in_out"
    kitchen = "kitchen"
    laundry = "laundry"
    medical_treatments = "medical_treatments"  # санаторные процедуры


class HotelKind(StrEnum):
    hotel = "hotel"
    resort = "resort"
    sanatorium = "sanatorium"
    guest_house = "guest_house"
    apartment = "apartment"
    hostel = "hostel"
    chalet = "chalet"
    motel = "motel"


class VibeTag(StrEnum):
    """«Настроение» отеля и поездки. NLU мапит «хочу спокойно/романтично/тусить» сюда."""

    quiet = "quiet"
    party = "party"
    family = "family"
    romantic = "romantic"
    business = "business"
    budget = "budget"
    luxury = "luxury"
    nature = "nature"
    sport = "sport"
    wellness = "wellness"


class MealPlan(StrEnum):
    RO = "RO"  # без питания
    BB = "BB"  # завтрак
    HB = "HB"  # полупансион
    FB = "FB"  # полный пансион
    AI = "AI"  # всё включено


class PetKind(StrEnum):
    dog = "dog"
    cat = "cat"
    other = "other"


class BudgetScope(StrEnum):
    total_trip = "total_trip"  # на всю поездку (по умолчанию — так сказали на Q&A)
    hotel_total = "hotel_total"  # только проживание
    per_night = "per_night"


class ArrivalMode(StrEnum):
    plane = "plane"
    train = "train"
    car = "car"
    unknown = "unknown"


class DistrictKind(StrEnum):
    sea = "sea"
    mountain = "mountain"
    city = "city"  # городской район без своего пляжа (Тбилиси, Даунтаун Дубая)


class Archetype(StrEnum):
    best_value = "best_value"  # самый выгодный из подходящих
    balanced = "balanced"  # лучший общий скор
    comfort = "comfort"  # максимум комфорта в рамках ограничений
    alternative = "alternative"


class Severity(StrEnum):
    info = "info"
    warning = "warning"
    blocker = "blocker"


class Intent(StrEnum):
    plan_trip = "plan_trip"  # первичный запрос / новая поездка
    modify = "modify"  # изменить условия («на 2 дня позже», «дешевле», «с собакой»)
    inspire = "inspire"  # «не знаю куда» → предложить районы
    ask_info = "ask_info"  # вопрос о варианте/районе/условиях
    compare = "compare"
    select_variant = "select_variant"
    book = "book"  # в корзину / оформить
    post_purchase = "post_purchase"  # ранний заезд, перенос дат, трансфер к заказу
    tickets = "tickets"  # «какие есть рейсы?», «сколько стоят билеты?», «во сколько вылет?»
    reset = "reset"
    smalltalk = "smalltalk"
    off_topic = "off_topic"
    unclear = "unclear"


class TripStage(StrEnum):
    collecting = "collecting"  # собираем критичные данные
    proposing = "proposing"  # показали варианты
    selected = "selected"  # выбран вариант
    in_cart = "in_cart"
    ordered = "ordered"


class CartItemKind(StrEnum):
    hotel_stay = "hotel_stay"
    ticket = "ticket"
    transfer = "transfer"
    early_check_in = "early_check_in"
    late_check_out = "late_check_out"


class OrderStatus(StrEnum):
    awaiting_payment = "awaiting_payment"
    paid = "paid"
    change_requested = "change_requested"
    cancelled = "cancelled"


class PostPurchaseKind(StrEnum):
    early_check_in = "early_check_in"
    late_check_out = "late_check_out"
    change_dates = "change_dates"
    add_transfer = "add_transfer"
    reminder = "reminder"
    cancel = "cancel"
