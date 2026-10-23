"""英語の単元・小単元・誤答の種類・読解の題材。web も読むので重い import をしない。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Reading:
    """読解の小単元。本文の長さ（語数）と設問の数、作成役に伝える本文の種類と設問の作り方。"""

    id: str
    label: str
    level: str
    words: tuple[int, int]
    questions: int
    genre: str
    question_style: str


@dataclass(frozen=True)
class Vocab:
    """語彙の小単元。単語リストのどのレベルの語を使うか。"""

    id: str
    label: str
    level: str
    fmt: str
    word_level: str


READINGS: dict[str, Reading] = {r.id: r for r in (
    Reading("en-notice", "お知らせ・広告・メール", "basic", (80, 150), 2,
            "a short practical text such as a notice, a poster, an advertisement, a schedule or an e-mail. "
            "Include concrete details (dates, times, prices, places, conditions)",
            "Ask about concrete information in the text (what, when, where, how much, what you must do). "
            "One question may ask what is true according to the text"),
    Reading("en-expository", "説明文", "common", (200, 300), 3,
            "an explanatory article of 3 or 4 paragraphs that explains a topic with facts, reasons and examples",
            "Ask about the main idea, a reason or cause, and a detail or example. "
            "Use forms like 'According to the article, ...', 'Why ...?', 'Which is true about ...?'"),
    Reading("en-story", "物語・体験談", "common", (200, 300), 3,
            "a story or a personal experience written in the past tense, with a clear order of events "
            "and the writer's feelings",
            "Ask about what happened, why a person did something, how a person felt or changed, "
            "and what happened first or last"),
)}

VOCABS: dict[str, Vocab] = {v.id: v for v in (
    Vocab("en-vocab-blank", "空所に入る語", "common", "choice", "common"),
    Vocab("en-vocab-meaning", "文中の語の意味", "basic", "choice", "basic"),
    Vocab("en-spelling", "スペル", "basic", "input", "basic"),
)}

UNITS = (
    ("en-reading", "読解", tuple(READINGS)),
    ("en-vocab", "語彙", tuple(VOCABS)),
)

# 選択式の誤答につける「間違いの種類」（回答の記録に残し、後のフェーズの得意・苦手の分析に使う）
MISTAKES = {
    "not_stated": "本文に書かれていない",
    "contradicts": "本文と食い違う",
    "partial": "一部だけ合っている",
    "detail": "数・日時・人などの取り違え",
    "other_word": "文脈に合わない別の語",
    "other_meaning": "別の語の意味",
}
READING_MISTAKES = ("not_stated", "contradicts", "partial", "detail")

# 読解の題材（同じ小単元で同じ題材は 1 回だけ使う。params_key に入れる）
TOPICS: dict[str, tuple[str, ...]] = {
    "en-notice": (
        "a poster for a school festival", "a library notice about new opening hours",
        "an e-mail from a host family before a homestay", "an advertisement for a summer English camp",
        "a notice about a community clean-up event", "a museum's special exhibition", "a lost and found notice",
        "a flyer for a cooking class", "a sports club looking for new members", "a change in a bus timetable",
        "a bike rental service for tourists", "volunteers wanted at an animal shelter",
        "concert tickets on sale", "the schedule of a school trip", "rules for recycling in an apartment building",
        "a language exchange event", "a photo contest for students", "a weekend farmers' market",
        "a science workshop for children", "a city marathon", "the opening of a new cafe",
        "a new smartphone app for a city's buses", "a night tour at a zoo", "an art class for beginners",
        "a used book sale", "a guided hiking tour", "a part-time job at a bakery",
        "a summer reading challenge at a library", "a swimming pool's summer schedule",
        "an e-mail from a teacher about a field trip",
    ),
    "en-expository": (
        "why some birds migrate", "how bees communicate", "the history of the bicycle",
        "the problem of food waste", "why sleep is important for students", "plastic in the ocean",
        "gardens in big cities", "the invention of paper", "how volcanoes form", "the importance of breakfast",
        "solar power", "the role of forests", "the history of chocolate", "why people yawn",
        "electric cars", "the history of the Olympic Games", "how rainbows appear",
        "school uniforms around the world", "online learning", "how human memory works", "coral reefs",
        "the history of tea", "robots in hospitals", "the benefits of reading books",
        "how desert animals survive", "how maps have changed", "fast fashion", "the power of music on mood",
        "wind energy", "the life of penguins", "the history of the umbrella", "working from home",
        "local festivals and tourism", "why leaves change color in autumn", "the history of trains",
        "how recycling works", "the benefits of learning a second language", "urban heat in summer",
        "the life of elephants", "vending machines",
    ),
    "en-story": (
        "a student who joins the drama club", "a girl who loses her wallet while traveling abroad",
        "a boy who learns to cook for his family", "a first homestay experience",
        "a runner who fails in a race and tries again", "a lost dog that is found",
        "an old letter from a grandmother", "a school band preparing for a concert",
        "a volunteer who reads books to children", "a mountain trip with bad weather",
        "a new transfer student", "a first part-time job at a cafe", "an exchange student's surprise at a new culture",
        "growing vegetables at school", "a misunderstanding between two friends", "a science fair project",
        "a bike trip around a lake", "helping an elderly neighbor", "learning to swim as a teenager",
        "a photography contest", "the captain of a soccer team", "a forgotten umbrella",
        "selling food at a school festival", "writing a speech for a contest", "a camping trip under the stars",
        "fixing an old piano", "a pen pal from another country", "a snowy day when the trains stopped",
        "a family moving to a new town", "a surprise birthday party for a younger brother",
    ),
}
