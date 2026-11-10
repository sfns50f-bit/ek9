"""公共の単元・小単元・資料の一覧・誤答の種類。web も読む（LLM・ネットワークなし）。

単元は学習指導要領の「公共」から（使用者本人に確認済み）。資料は小単元ごとに、e-Gov の法令（条の番号）と
Wikipedia 日本語版の記事（節）を並べる。資料 1 つが本文 1 つ（設問 2〜3 問）になる。
資料を足すときは `SOURCES` に 1 行足す（法令は e-Gov の法令 ID と条の番号。「第一条の二」は "1_2"）。
記事・条が見つからない資料は作成のときに飛ばす（作成の記録に「資料を取れなかった」と残る）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 誤答につける「間違いの種類」（回答記録に残す。後のフェーズの得意・苦手の分析用）
MISTAKES = {
    "contradicts": "資料と食い違う",
    "not_stated": "資料に書かれていない",
    "partial": "一部だけ合っている",
    "detail": "主体・時期・数などの取り違え",
}


@dataclass(frozen=True)
class Source:
    """資料 1 つ。kind="law"：key＝e-Gov の法令 ID、parts＝条の番号。kind="wiki"：key＝記事名、section＝節（空は導入から）。"""

    kind: str
    key: str
    parts: tuple[str, ...] = ()
    section: str = ""
    label: str = ""
    law_title: str = ""  # 法令の表示名（e-Gov の題名が長い・分かりにくいときだけ）

    @property
    def id(self) -> str:
        """題材の名前（passages.topic・重複の判定に使う）。"""
        if self.kind == "law":
            return f"law:{self.key}:{','.join(self.parts)}"
        return f"wiki:{self.key}" + (f"#{self.section}" if self.section else "")

    @property
    def name(self) -> str:
        return self.label or (self.key + (f"（{self.section}）" if self.section else ""))


KENPO = "321CONSTITUTION"


def law(key: str, *parts: str, label: str = "", title: str = "") -> Source:
    return Source("law", key, tuple(parts), label=label, law_title=title)


def kenpo(*parts: str, label: str = "") -> Source:
    return law(KENPO, *parts, label=label or f"日本国憲法 第{'・'.join(parts)}条", title="日本国憲法")


def wiki(title: str, section: str = "") -> Source:
    return Source("wiki", title, section=section)


@dataclass(frozen=True)
class Spec:
    id: str
    label: str
    level: str  # base.LEVELS のキー
    focus: str  # 設問の作り方（作成役に渡す）


# (単元ID, 単元名, [小単元]) 。小単元の ID は cv- で始める
UNITS: list[tuple[str, str, list[Spec]]] = [
    ("cv-door", "公共の扉", [
        Spec("cv-self", "青年期と自己形成", "basic", "青年期の特徴・心理学の用語の意味と、その用語が表す具体的な場面"),
        Spec("cv-ethics", "思想と倫理", "common", "思想家・考え方の主張の中身と、考え方どうしの違い"),
    ]),
    ("cv-law", "法と私たち", [
        Spec("cv-rights", "憲法と人権", "basic", "人権の種類と、条文が保障している内容・その限界"),
        Spec("cv-contract", "契約と消費者", "basic", "契約が成り立つ・取り消せる条件、消費者を守る制度の要件"),
        Spec("cv-justice", "司法と裁判員", "common", "裁判のしくみ・司法権の独立・裁判員制度の要件"),
    ]),
    ("cv-politics", "政治", [
        Spec("cv-govern", "国会・内閣・裁判所", "basic", "国会・内閣・裁判所の権限と、そのあいだの関係"),
        Spec("cv-election", "選挙と政党", "common", "選挙の制度の要件・しくみと、制度ごとの特徴"),
        Spec("cv-local", "地方自治", "basic", "地方自治のしくみ・住民の権利（直接請求など）の要件"),
    ]),
    ("cv-economy", "経済", [
        Spec("cv-market", "市場のしくみ", "basic", "市場のしくみ・市場の失敗・企業の用語の意味"),
        Spec("cv-finance", "財政と税", "common", "財政の役割・税の種類と特徴・国債のルール"),
        Spec("cv-money", "金融", "common", "金融のしくみ・中央銀行の役割と金融政策"),
        Spec("cv-labor", "雇用と労働", "basic", "働く人の権利と、法律が定める労働条件"),
        Spec("cv-welfare", "社会保障", "common", "社会保障の種類・しくみと、その目的"),
    ]),
    ("cv-world", "国際社会", [
        Spec("cv-intl-politics", "国際政治", "common", "国際機関・国際法のしくみと権限"),
        Spec("cv-intl-economy", "国際経済", "common", "貿易・為替・国際経済のしくみと用語の意味"),
    ]),
]

SPECS: dict[str, Spec] = {s.id: s for _u, _l, specs in UNITS for s in specs}

# 小単元ごとの資料（2026-11 に e-Gov の法令 ID と条を確かめた。Wikipedia の記事名は実機の取得で確かめる）。
# 人物の記事は、導入が生い立ちになりやすいので、考え方を説明した記事・節を選ぶ（人物の記事を足すなら節を指定する）
SOURCES: dict[str, list[Source]] = {
    "cv-self": [wiki("青年期"), wiki("アイデンティティ"), wiki("自己実現理論"), wiki("防衛機制"), wiki("通過儀礼"),
                wiki("マージナル・マン"), wiki("欲求")],  # 人物の記事（エリクソン）は生い立ちが選ばれたので外した（第 14 回 A-3）
    "cv-ethics": [wiki("功利主義"), wiki("義務論"), wiki("社会契約"), wiki("公共の福祉"), wiki("ジョン・ロールズ"),
                  wiki("正義論"), wiki("自由主義"), wiki("ジョン・スチュアート・ミル")],
    "cv-rights": [kenpo("11", "12", "13"), kenpo("14"), kenpo("19", "20", "21"), kenpo("22", "29"), kenpo("25", "26"),
                  kenpo("31", "36", "37", "38"), law("415AC0000000057", "1", title="個人情報の保護に関する法律"),
                  wiki("基本的人権"), wiki("法の下の平等"), wiki("表現の自由"), wiki("生存権"), wiki("新しい人権"),
                  wiki("プライバシー")],
    "cv-contract": [law("129AC0000000089", "4", "5", title="民法"),
                    law("412AC0000000061", "1", "2", title="消費者契約法"),
                    law("412AC0000000061", "4", title="消費者契約法"),
                    law("351AC0000000057", "9", title="特定商取引に関する法律"),
                    law("343AC1000000078", "1", "2", title="消費者基本法"),
                    wiki("契約"), wiki("クーリング・オフ"), wiki("成年"), wiki("消費者契約法"), wiki("製造物責任法")],
    "cv-justice": [kenpo("76", "77", "78"), kenpo("79", "80", "81", "82"),
                   law("416AC0000000063", "1", "13", title="裁判員の参加する刑事裁判に関する法律"),
                   law("416AC0000000063", "2", title="裁判員の参加する刑事裁判に関する法律"),
                   wiki("裁判員制度"), wiki("三審制"), wiki("違憲審査制"), wiki("司法権の独立"), wiki("検察審査会")],
    "cv-govern": [kenpo("41", "42", "43"), kenpo("59", "60", "61"), kenpo("62", "63", "64"), kenpo("65", "66", "67", "68"),
                  kenpo("69", "70", "71"), kenpo("72", "73"), wiki("三権分立"), wiki("議院内閣制"), wiki("衆議院の優越"),
                  wiki("国政調査権"), wiki("内閣不信任決議")],
    "cv-election": [kenpo("15", "44", "47"), law("325AC1000000100", "9", "10", title="公職選挙法"),
                    wiki("小選挙区比例代表並立制"), wiki("一票の格差"), wiki("政党"), wiki("普通選挙"),
                    wiki("期日前投票"), wiki("政党交付金")],
    "cv-local": [kenpo("92", "93", "94", "95"), law("322AC0000000067", "1_2", title="地方自治法"),
                 law("322AC0000000067", "74", title="地方自治法"), law("322AC0000000067", "76", title="地方自治法"),
                 wiki("地方自治"), wiki("直接請求"), wiki("住民投票"), wiki("地方交付税"), wiki("地方分権")],
    "cv-market": [law("322AC0000000054", "1", "3", title="独占禁止法（私的独占の禁止及び公正取引の確保に関する法律）"),
                  wiki("市場経済"), wiki("市場の失敗"), wiki("寡占"), wiki("外部性"), wiki("需要と供給"),
                  wiki("公正取引委員会"), wiki("株式会社")],
    "cv-finance": [kenpo("83", "84", "85", "86"), law("322AC0000000034", "4", title="財政法"), wiki("財政政策"),
                   wiki("累進課税"), wiki("消費税"), wiki("国債"), wiki("直接税"), wiki("租税法律主義")],
    "cv-money": [law("409AC0000000089", "1", "2", title="日本銀行法"), wiki("金融政策"), wiki("日本銀行"),
                 wiki("信用創造"), wiki("公開市場操作"), wiki("インフレーション"), wiki("デフレーション"),
                 wiki("間接金融")],
    "cv-labor": [kenpo("27", "28"), law("322AC0000000049", "1", "32", title="労働基準法"),
                 law("324AC0000000174", "1", title="労働組合法"),
                 law("347AC0000000113", "5", "6", title="男女雇用機会均等法（雇用の分野における男女の均等な機会及び待遇の確保等に関する法律）"),
                 wiki("労働三権"), wiki("最低賃金"), wiki("非正規雇用"), wiki("ワーク・ライフ・バランス"), wiki("労働基準法")],
    "cv-welfare": [kenpo("25"), law("325AC0000000144", "1", "4", title="生活保護法"), wiki("社会保障"),
                   wiki("国民皆保険"), wiki("公的年金"), wiki("社会保険"), wiki("介護保険"), wiki("少子高齢化")],
    "cv-intl-politics": [kenpo("9"), wiki("国際連合"), wiki("国際連合安全保障理事会"), wiki("国際司法裁判所"),
                         wiki("国際法"), wiki("核拡散防止条約"), wiki("難民"), wiki("国際連合平和維持活動")],
    "cv-intl-economy": [wiki("比較優位"), wiki("世界貿易機関"), wiki("為替レート"), wiki("国際収支"),
                        wiki("経済連携協定"), wiki("政府開発援助"), wiki("南北問題")],
}


def questions_for(body: str) -> int:
    """本文 1 つにつける設問の数（短い条文は 2 問、長い本文は 3 問）。"""
    return 3 if len(body) >= 700 else 2
