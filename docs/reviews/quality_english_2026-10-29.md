# 検証の成績

期間：30 日・依頼 #147 以降（検証のテストは含めない）

作成 212 問のうち合格 111 問（52%）

## 作成役のモデルごと

| 作成役 | 作った数 | 合格 | 不合格の内訳 |
|---|---|---|---|
| gemma4:26b-a4b-it-qat | 150 | 78（52%） | 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など） 29、検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める） 20、検証で、正解のほかにも合う選択肢・語があった（正解が 1 つに決まらない） 10、見直しで誤りを指摘された（解説・ヒント・訳の誤り、例文・本文の筋） 6、本文を読まなくても解けた（常識で答えられる設問） 3、検証役の答えが読み取れなかった 2、検証役の答えは読めたが、数値が正解と違う 1、本文の見直しで誤りを指摘された（時系列・因果・矛盾・英文の誤り） 1 |
| anthropic/claude-haiku-5-5 | 62 | 33（53%） | 本文を読まなくても解けた（常識で答えられる設問） 12、見直しで誤りを指摘された（解説・ヒント・訳の誤り、例文・本文の筋） 7、検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める） 5、本文の見直しで誤りを指摘された（時系列・因果・矛盾・英文の誤り） 4、英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など） 1 |

## 検証役のモデルごと

| 検証役 | 検証した数 | 弾いた数 | 弾いた割合 |
|---|---|---|---|
| gemma4:e4b-it-qat | 94 | 46 | 49% |
| anthropic/claude-haiku-5-5 | 45 | 5 | 11% |

## 不合格の理由

| 理由 | 数 |
|---|---|
| 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など） | 30 |
| 検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める） | 25 |
| 本文を読まなくても解けた（常識で答えられる設問） | 15 |
| 見直しで誤りを指摘された（解説・ヒント・訳の誤り、例文・本文の筋） | 13 |
| 検証で、正解のほかにも合う選択肢・語があった（正解が 1 つに決まらない） | 10 |
| 本文の見直しで誤りを指摘された（時系列・因果・矛盾・英文の誤り） | 5 |
| 検証役の答えが読み取れなかった | 2 |
| 検証役の答えは読めたが、数値が正解と違う | 1 |

## 依頼ごとの比較

| 依頼 | 作成役 | 検証役 | 合格／作成 | 作成 1 問（秒） | 検証 1 問（秒） | 読み込み（秒） | API（ドル） | 不合格 |
|---|---|---|---|---|---|---|---|---|
| #158 | gemma4:26b-a4b-it-qat | gemma4:e4b-it-qat ×2 | 5／36（14%） | 8.4 | 55.9 | 211.6 | — | 既存の問題と同じ数値 23、英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など） 4、検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める） 2、本文の見直しで誤りを指摘された（時系列・因果・矛盾・英文の誤り） 1、見直しで誤りを指摘された（解説・ヒント・訳の誤り、例文・本文の筋） 1 |
| #157 | gemma4:26b-a4b-it-qat | gemma4:e4b-it-qat ×2 | 10／27（37%） | 68.0 | 81.7 | 292.1 | — | 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など） 11、本文を読まなくても解けた（常識で答えられる設問） 3、検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める） 2、検証役の答えは読めたが、数値が正解と違う 1 |
| #156 | gemma4:26b-a4b-it-qat | gemma4:e4b-it-qat ×2 | 10／24（42%） | 67.5 | 53.9 | 305.1 | — | 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など） 10、検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める） 3、見直しで誤りを指摘された（解説・ヒント・訳の誤り、例文・本文の筋） 1 |
| #155 | gemma4:26b-a4b-it-qat | gemma4:e4b-it-qat ×2 | 10／19（53%） | 5.4 | 42.5 | 96.3 | — | 検証で、正解のほかにも合う選択肢・語があった（正解が 1 つに決まらない） 5、検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める） 3、見直しで誤りを指摘された（解説・ヒント・訳の誤り、例文・本文の筋） 1 |
| #154 | gemma4:26b-a4b-it-qat | gemma4:e4b-it-qat ×2 | 10／30（33%） | 5.3 | 30.6 | 192.2 | — | 検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める） 7、検証で、正解のほかにも合う選択肢・語があった（正解が 1 つに決まらない） 5、英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など） 4、見直しで誤りを指摘された（解説・ヒント・訳の誤り、例文・本文の筋） 2、検証役の答えが読み取れなかった 2 |
| #153 | gemma4:26b-a4b-it-qat | gemma4:e4b-it-qat ×2 | 10／14（71%） | 2.6 | 30.3 | 94.0 | — | 検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める） 3、見直しで誤りを指摘された（解説・ヒント・訳の誤り、例文・本文の筋） 1 |
| #152 | anthropic/claude-haiku-5-5 | anthropic/claude-haiku-5-5 ×2 | 5／6（83%） | 4.1 | 8.8 | 0.0 | 0.006 | 見直しで誤りを指摘された（解説・ヒント・訳の誤り、例文・本文の筋） 1 |
| #151 | anthropic/claude-haiku-5-5 | anthropic/claude-haiku-5-5 ×2 | 5／7（71%） | 7.3 | 11.7 | 0.0 | 0.011 | 見直しで誤りを指摘された（解説・ヒント・訳の誤り、例文・本文の筋） 1、検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める） 1 |
| #150 | anthropic/claude-haiku-5-5 | anthropic/claude-haiku-5-5 ×2 | 5／8（62%） | 23.0 | 11.9 | 0.0 | 0.017 | 本文の見直しで誤りを指摘された（時系列・因果・矛盾・英文の誤り） 2、見直しで誤りを指摘された（解説・ヒント・訳の誤り、例文・本文の筋） 1 |
| #149 | anthropic/claude-haiku-5-5 | anthropic/claude-haiku-5-5 ×2 | 7／22（32%） | 34.8 | 13.8 | 0.0 | 0.045 | 本文を読まなくても解けた（常識で答えられる設問） 12、本文の見直しで誤りを指摘された（時系列・因果・矛盾・英文の誤り） 1、見直しで誤りを指摘された（解説・ヒント・訳の誤り、例文・本文の筋） 1、英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など） 1 |
| #148 | anthropic/claude-haiku-5-5 | anthropic/claude-haiku-5-5 ×2 | 6／13（46%） | 16.1 | 10.6 | 0.0 | 0.024 | 見直しで誤りを指摘された（解説・ヒント・訳の誤り、例文・本文の筋） 3、検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める） 3、本文の見直しで誤りを指摘された（時系列・因果・矛盾・英文の誤り） 1 |
| #147 | anthropic/claude-haiku-5-5 | anthropic/claude-haiku-5-5 ×2 | 5／6（83%） | 3.8 | 6.7 | 0.0 | 0.005 | 検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める） 1 |

## 不合格の一覧

### #1318 見直しで誤りを指摘された（解説・ヒント・訳の誤り、例文・本文の筋）（10/29 21:56）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：解説の見直し（gemma4:12b-it-qat）
- 理由：gemma4:12b-it-qat: 誤りを指摘：解説・例文（解説の「他の選択肢については、...ウェブサイトのURLもチケット購入用ではなく情報提供用（info@...）として示されているため誤りです」という記述に誤りがあります。

本文には「Tickets can be purchased online at www.example.org/concert）
- 問題文：How can people get tickets for the concert?

```
【解説の点検】
[[verdict]]
問題あり
[[issues]]
解説の「他の選択肢については、...ウェブサイトのURLもチケット購入用ではなく情報提供用（info@...）として示されているため誤りです」という記述に誤りがあります。

本文には「Tickets can be purchased online at www.example.org/concert-tickets」とあり、このURLはチケット購入用です。解説では「info@...」を指していますが、設問の選択肢にあるURLは「www.example.org」であり、本文の購入用URLの一部（ドメイン部分）を含んでいます。

また、選択肢の「By visiting the Green Valley Hall website at www.example.org.」が誤りである理由は、URLが「購入用」ではないからではなく、本文にある購入用URLは「www.example.org/concert-tickets」であり、選択肢のURLは不完全（または異なるページを指す）だからです。解説の論理構成を修正する必要があります。
```

### #1317 本文の見直しで誤りを指摘された（時系列・因果・矛盾・英文の誤り）（10/29 21:54）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：本文の見直し（gemma4:12b-it-qat）
- 理由：gemma4:12b-it-qat: 本文の見直しで誤りを指摘された（1. 時系列の矛盾：冒頭で「チケットは来週（next week）発売される」と述べていますが、詳細欄では「6月10日（Monday, June 10th）」から発売とあり、この文章が書かれた時点（現在）がいつなのかによって「来週」と一致しない可能性があります。
2. 時系列の矛盾：イベントの開催日は「8月15日〜17日」ですが、早期割引（early bird tickets）の期限が「6月20日」となっており、発売開始（6月10日）からわずか10日間しか猶予がない設定になっています。通常、数ヶ月先のイベントに対して、発売直後に終わってしまう早期割引設定は不自然です。）
- 問題文：（本文の段階で不合格）Summer Jazz Festival Ticket Sales

```
[[verdict]]
問題あり
[[issues]]
1. 時系列の矛盾：冒頭で「チケットは来週（next week）発売される」と述べていますが、詳細欄では「6月10日（Monday, June 10th）」から発売とあり、この文章が書かれた時点（現在）がいつなのかによって「来週」と一致しない可能性があります。
2. 時系列の矛盾：イベントの開催日は「8月15日〜17日」ですが、早期割引（early bird tickets）の期限が「6月20日」となっており、発売開始（6月10日）からわずか10日間しか猶予がない設定になっています。通常、数ヶ月先のイベントに対して、発売直後に終わってしまう早期割引設定は不自然です。
```

### #1315 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 21:54）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: 誤答「Lunch after the cooking class」の種類が not_stated だが、中心の語が本文にある（本文と食い違うなら contradicts、一部だけ違うなら partial、細部の違いなら detail に直す）
- 問題文：What is included in the price for the cooking class?

### #1314 検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める）（10/29 21:53）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：検証（gemma4:e4b-it-qat）
- 理由：検証の判定が割れた（1/2 回合格）：gemma4:e4b-it-qat: 正解の裏付け（正解「Reusable bags for their purchases.」は本文に書かれていない（引用: "We encourage you to bring your own reusable bags to help reduce waste." — 持参は推奨されているだけで、必須（nece）／1 対 1 の判定（「Eco-friendly bags to receive food.」と比べて誤答の方を選んだ：本文には「we only accept... eco-friendly bags」とあり、エコフレンドリーなバッグが必須であると書かれている。（正解が複数の疑い））
- 問題文：According to the text, what is necessary for visitors to bring?

```
【選択肢の判定】
[[judgments]]
1: 本文と食い違う — 引用: "Admission: Free" — 入場は無料であるため、お金は必要ではない。
2: 本文に書かれていない — 引用: "Please note that we only accept cash and eco-friendly bags." — エコバッグは受け入れるものとして挙げられているが、訪問者が持参する必要があるとは書かれていない。
3: 本文に書かれていない — 引用: "We encourage you to bring your own reusable bags to help reduce waste." — 持参は推奨されているだけで、必須（necessary）であるとは述べられていない。
4: 本文と食い違う — 引用: "Please note that we only accept cash and eco-friendly bags." — クレジットカードは受け付けない。

【1 対 1 の判定】
[[answer]]
1
[[reason]]
本文には「we only accept... eco-friendly bags」とあり、エコフレンドリーなバッグが必須であると書かれている。
```

### #1313 検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める）（10/29 21:53）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：検証（gemma4:e4b-it-qat）
- 理由：検証の判定が割れた（1/2 回合格）：gemma4:e4b-it-qat: 正解が 1 つか（「Buy handmade items at the Main Plaza.」も本文で裏付けられる（正解が複数））
- 問題文：What can people do at the market?

```
【選択肢の判定】
[[judgments]]
1: 一部だけ合う — 引用: "We will have many organic vegetables, fresh fruits, handmade cheeses, and delicious baked goods." — 有機食品は土曜日だけでなく日曜日も購入可能である
2: 裏付けあり — 引用: "We will have many organic vegetables, fresh fruits, handmade cheeses, and delicious baked goods." — メインプラザで手作りチーズなどの購入が可能である
3: 裏付けあり — 引用: "There will also be live music and a small workshop for children at 11:00 a.m. each day." — 毎日午前11時に子供向けのワークショップに参加できる
4: 本文に書かれていない — 引用: (なし) — 料理教室に関する記述は本文にはない
```

### #1312 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 21:51）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: 解説が選択肢を A・B などの記号で呼んでいる（選択肢は並べ替えるので中身で呼ぶ）
- 問題文：What is true about the job at Sunny Side Bakery?

### #1311 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 21:51）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: 解説が選択肢を A・B などの記号で呼んでいる（選択肢は並べ替えるので中身で呼ぶ）
- 問題文：What is required for those who want to work at Sunny Side Bakery?

### #1309 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 21:50）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: 誤答「A textbook for learning basic techniques」の種類が not_stated だが、中心の語が本文にある（本文と食い違うなら contradicts、一部だけ違うなら partial、細部の違いなら detail に直す）
- 問題文：What is included in the $50 fee for the course?

### #1304 本文を読まなくても解けた（常識で答えられる設問）（10/29 21:45）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：本文を見せずに解かせたら正解した（常識で答えられる設問なので使わない）
- 問題文：Why might festivals cause problems for local residents?

### #1302 検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める）（10/29 21:26）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：検証（gemma4:e4b-it-qat）
- 理由：検証の判定が割れた（1/2 回合格）：gemma4:e4b-it-qat: 正解が 1 つか（「It protects the young from being lost in the wild.」も本文で裏付けられる（正解が複数））／1 対 1 の判定（「It protects the young from being lost in the wild.」と比べて両方成り立つ：第1段落で食料と水の情報を共有し、第3段落で子どもの安全を確保することが述べられている。（正解が複数の疑い））
- 問題文：According to the article, why is the social structure of elephant herds important?

```
【選択肢の判定】
[[judgments]]
[[
1: 裏付けあり — 引用: "This social organization is essential for survival because it allows the group to share information about food and water sources, which are often located far away in the wild." — 理由: 情報共有により食料や水を見つけやすくなるため。
2: 本文に書かれていない — 理由: マトリアーキが群れを効果的に率いるという因果関係は本文にない。
3: 本文に書かれていない — 理由: 他の群れとのコミュニケーションに関する記述であり、群れのメンバーを見つけることではない。
4: 裏付けあり — 引用: "By using these sophisticated methods, elephants can coordinate their movements and ensure the safety of their young, demonstrating how vital communication is to their survival." — 理由: 群れの安全を確保することで、若い個体の安全が守られるため。
]]

【1 対 1 の判定】
[[answer]]
[[両方]]
[[本文では、社会構造が食料や水の情報を共有すること、および遠距離の群れとコミュニケーションをとることを可能にしていることが述べられている。]]

【1 対 1 の判定】
[[answer]]
1
[[reason]]
本文の第1段落に、社会構造が食物と水に関する情報を共有することを可能にし、生存に不可欠であると記載されている。

【1 対 1 の判定】
[[answer]]
[[両方]]
[[第1段落で食料や水場の情報の共有が生存に不可欠であると述べられており、第3段落で子たちの安全を確保できると述べられている。]]

【1 対 1 の判定】
[[answer]]
両方
[[reason]]
第1段落で食料と水の情報を共有し、第3段落で子どもの安全を確保することが述べられている。
```

### #1301 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 21:26）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: 誤答「The group immediately moves to a new water source.」の種類が not_stated だが、中心の語が本文にある（本文と食い違うなら contradicts、一部だけ違うなら partial、細部の違いなら detail に直す）
- 問題文：What can be observed when a member of an elephant herd dies?

### #1299 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 21:24）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: 誤答「They stay with the chicks in large groups.」の種類が not_stated だが、中心の語が本文にある（本文と食い違うなら contradicts、一部だけ違うなら partial、細部の違いなら detail に直す）
- 問題文：Which is true about some male penguins when raising their young?

### #1298 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 21:24）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: 誤答「To find more food in the ocean.」の種類が not_stated だが、中心の語が本文にある（本文と食い違うなら contradicts、一部だけ違うなら partial、細部の違いなら detail に直す）／誤答「To find warmer areas to live.」の種類が not_stated だが、中心の語が本文にある（本文と食い違うなら contradicts、一部だけ違うなら partial、細部の違いなら detail に直す）／誤答「To help the female find food.」の種類が not_stated だが、中心の語が本文にある（本文と食い違うなら contradicts、一部だけ違うなら partial、細部の違いなら detail に直す）
- 問題文：Why do many penguins stay in large groups called colonies?

### #1297 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 21:22）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: ヒントの「第2段落」が、根拠の文のある段落（第3段落）と違う
- 問題文：According to the article, which is true about swallows?

### #1296 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 21:22）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: ヒントの「第2段落」が、根拠の文のある段落（第3段落）と違う
- 問題文：Why do some birds fly to warmer areas during the winter?

### #1295 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 21:22）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: ヒントの「第1段落」が、根拠の文のある段落（第2段落）と違う
- 問題文：What is the main idea of the article?

### #1294 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 21:21）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: 解説が選択肢を A・B などの記号で呼んでいる（選択肢は並べ替えるので中身で呼ぶ）
- 問題文：Which is true about "range anxiety"?

### #1293 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 21:21）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: 誤答「Because the initial cost of EVs is decreasing.」の種類が not_stated だが、中心の語が本文にある（本文と食い違うなら contradicts、一部だけ違うなら partial、細部の違いなら detail に直す）／解説が選択肢を A・B などの記号で呼んでいる（選択肢は並べ替えるので中身で呼ぶ）
- 問題文：Why is it becoming more affordable for people to buy electric vehicles?

### #1292 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 21:21）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: 解説が選択肢を A・B などの記号で呼んでいる（選択肢は並べ替えるので中身で呼ぶ）
- 問題文：According to the article, why are more drivers choosing electric vehicles?

### #1291 検証役の答えは読めたが、数値が正解と違う（10/29 21:19）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：検証（gemma4:e4b-it-qat）
- 理由：gemma4:e4b-it-qat: 正解の裏付け（正解「Red is the color that bends the most.」は本文と食い違う（引用: "For example, red light bends the least, while violet light bends the most." — 赤は最も曲がりが少ないと本文に書か）／1 対 1 の判定（「Violet is the color that bends the least.」と比べて両方成り立つ：本文には「red light bends the least, while violet light bends the most.」とあり、どちらの選択肢も本文の内容と矛盾しているため、どち）
- 問題文：What is the correct order of colors in a rainbow?

```
【選択肢の判定】
[[judgments]]
1: 本文と食い違う — 引用: "For example, red light bends the least, while violet light bends the most." — 赤は最も曲がるのではなく、最も曲がらない
2: 本文と食い違う — 引用: "Because of this, the colors always appear in the same order: red is on the outside of the arc, and violet is on the inside." — 赤は外側にある
3: 本文と食い違う — 引用: "Because of this, the colors always appear in the same order: red is on the outside of the arc, and violet is on the inside." — 紫は内側にある
4: 本文と食い違う — 引用: "For example, red light bends the least, while violet light bends the most." — 紫は最も曲がる
```

### #1289 本文を読まなくても解けた（常識で答えられる設問）（10/29 21:19）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：本文を見せずに解かせたら正解した（常識で答えられる設問なので使わない）
- 問題文：Which is true about the conditions needed to see a rainbow?

### #1287 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 21:17）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: 解説が空
- 問題文：Which is true about the debate over school uniforms?

### #1286 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 21:17）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: 解説が空
- 問題文：Why do some schools in the United Kingdom use uniforms?

### #1283 本文を読まなくても解けた（常識で答えられる設問）（10/29 21:15）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：本文を見せずに解かせたら正解した（常識で答えられる設問なので使わない）
- 問題文：Which of the following is true about the challenges of online learning?

### #1282 検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める）（10/29 21:13）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：検証（gemma4:e4b-it-qat）
- 理由：検証の判定が割れた（1/2 回合格）：gemma4:e4b-it-qat: 判定を読めなかった（選択肢の判定：本文にない文を引用した・引用のない判定が残った（引用なし）／1 対 1 の判定：判定を読めなかった（「To offer a variety of healthy food options.」））
- 問題文：What is the main purpose of vending machines according to the text?

```
【選択肢の判定】
[[judgments]]
[[
1: 裏付けあり — "While the technology and variety continue to grow, the basic purpose of vending machines remains the same: to provide quick and easy access to products at any time of the day." — 自動販売機の基本的な目的は「いつでも簡単に製品にアクセスできるようにすること」と明記されている。
2: 一部だけ合う — "For example, many machines now use advanced sensors to detect which product a customer is looking at, allowing them to play advertisements on a built-in screen." — 広告表示は機能の一つではあるが、それが主な目的ではない。
3: 一部だけ合う — "In some areas, specialized machines provide healthy options like salads or fresh fruit to support people with busy lifestyles." — 健康的な選択肢の提供は一部の地域で行われる機能の一つである。
4: 本文に書かれていない — — 自動販売機が伝統的な店舗を置き換えるという内容は本文には書かれていない。
]]

【選択肢の判定】
[[judgments]]
1: 裏付けあり — While the technology and variety continue to grow, the basic purpose of vending machines remains the same: to provide quick and easy access to products at any time of the day. — 本文の「基本的な目的」として、いつでも簡単にアクセスできるようにすることと明記されている。
2: 本文に書かれていない — — 広告表示は機能の一つであり、主な目的ではない。
3: 一部だけ合う — In some areas, specialized machines provide healthy options like salads or fresh fruit to support people with busy lifestyles. — 健康的な選択肢を提供する機能は存在するが、それが主な目的ではない。
4: 本文に書かれていない — — 伝統的な店舗を置き換えるという記述はない。

【選択肢の判定】
[[judgments]]
[[
1: 裏付けあり — While the technology and variety continue to grow, the basic purpose of vending machines remains the same: to provide quick and easy access to products at any time of the day. — 本文の最後の段落で、自動販売機の基本的な目的が「迅速かつ容易に製品を提供すること」であると述べられている。
2: 一部だけ合う 
```

### #1279 検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める）（10/29 21:10）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：検証（gemma4:e4b-it-qat）
- 理由：検証の判定が割れた（1/2 回合格）：gemma4:e4b-it-qat: 正解の裏付け（正解「He was surprised and showed a big smile.」は一部だけ合う（引用: "At first, he looked completely confused, but then a huge smile spread across his face." — 最初の）
- 問題文：How did Leo react when he first entered the room?

```
【選択肢の判定】
[[judgments]]
1: 一部だけ合う — 引用: "At first, he looked completely confused, but then a huge smile spread across his face." — 最初の反応は「confused」であり、「surprised」という表現は本文にないため
2: 本文に書かれていない — —
3: 本文に書かれていない — —
4: 本文に書かれていない — —
```

### #1277 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 21:10）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: 根拠の文「When we heard his school bus arrive, we quickly turned off all the lights and wa」が本文にそのまま入っていない（本文から一字一句写す）
- 問題文：What was the sequence of events on the day of the party?

### #1276 検証の判定が割れた（要確認。問題検証モードで確かめてストックに入れるか決める）（10/29 21:05）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：検証（gemma4:e4b-it-qat）
- 理由：検証の判定が割れた（1/2 回合格）：gemma4:e4b-it-qat: 判定を読めなかった（選択肢の判定：本文にない文を引用した・引用のない判定が残った（To my surprise, I did not win the grand prize, but I receive））
- 問題文：What was the result of the photography contest for the writer?

```
【選択肢の判定】
[[judgments]]
[[1: 本文と食い違う — 引用: "To my surprise, I did not win the grand prize, but I received a special mention for 'Most Peaceful Atmosphere.'" — 大賞は受賞していないため"
2: 本文に書かれていない — 引用: なし — 来年のコンテストを楽しみにしているため
3: 裏付けあり — 引用: "I received a special mention for 'Most Peaceful Atmosphere.'" — 雰囲気に関する特別賞を受賞したため
4: 本文と食い違う — 引用: "Last summer, I decided to enter a local photography contest called 'Nature in Our Town.'" — 昨年夏、コンテストに参加しているため]]

【選択肢の判定】
[[judgments]]
[[
1: 本文と食い違う — 引用: "To my surprise, I did not win the grand prize, but I received a special mention for 'Most Peaceful Atmosphere.'" — 本文ではグランプリは受賞していない。
2: 本文と食い違う — 引用: "I am already looking forward to next year's contest." — 次のコンテストも楽しみにしている。
3: 裏付けあり — 引用: "To my surprise, I did not win the grand prize, but I received a special mention for 'Most Peaceful Atmosphere.'" — 雰囲気に関する賞を受賞している。
4: 本文と食い違う — 引用: "Last summer, I decided to enter a local photography contest called 'Nature in Our Town.'" — 参加を決めていた。
]]

【選択肢の判定】
[[judgments]]
1: 本文と食い違う — 引用: "To my surprise, I did not win the grand prize, but I received a special mention for 'Most Peaceful Atmosphere.'" — 大賞は受賞していない。
2: 本文と食い違う — 引用: "I am already looking forward to next year's contest." — 来年のコンテストを楽しみにしている。
3: 裏付けあり — 引用: "To my surprise, I did not win the grand prize, but I received a special mention for 'Most Peaceful Atmosphere.'" — 写真の雰囲気に関する特別賞を受賞している。
4: 本文と食い違う — 引用: "Last summer, I decided to enter a local photography contest called 'Nature in O
```

### #1274 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 21:05）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: 誤答「To learn more about different types of local nature.」の種類が not_stated だが、中心の語が本文にある（本文と食い違うなら contradicts、一部だけ違うなら partial、細部の違いなら detail に直す）
- 問題文：Why did the writer participate in the "Nature in Our Town" contest?

### #1271 英文・選択肢・解説の点検で落ちた（根拠の文が本文にない・日本語の混入・選択肢の重複など）（10/29 20:53）

- 作成役：gemma4:26b-a4b-it-qat／落ちた段階：設問（—）
- 理由：設問の点検で落ちた: 誤答「They began to read the books more slowly.」の種類が not_stated だが、中心の語が本文にある（本文と食い違うなら contradicts、一部だけ違うなら partial、細部の違いなら detail に直す）
- 問題文：What did the writer do to improve their reading style?
