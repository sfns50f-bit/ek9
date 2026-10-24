"""中学校で習う英単語（第 4 回のレビュー D-1：高校の語彙の問題から外すため）。

中学の教科書・学習指導要領でよく使われる基本語を、語形（つづり）だけで並べたもの。
語彙・スペルの問題の正解には、ここにある語を使わない（易しすぎて練習にならない）。
"""

from __future__ import annotations

JHS_WORDS = frozenset("""
a about above abroad across act action activity actor actually add address adult advice afraid after afternoon
again against age ago agree air airplane airport album all almost alone along already also always am among and
angry animal another answer any anyone anything apple April are area arm around arrive art artist as ask at
attention August aunt autumn away baby back bad bag ball banana band bank baseball basket basketball bath be
beach bear beautiful because become bed bedroom bee before begin behind believe bell best better between big
bike bird birthday black blue boat body book bored born borrow both bottle bottom box boy brave bread break
breakfast bridge bright bring brother brown build building bus business busy but buy by cake call camera camp
can cap captain car card care careful carry case cash castle cat catch center century chair chance change
cheap cheese chicken child children choose church city class classmate classroom clean clear climb clock
close clothes cloud cloudy club coat coffee cold collect college color come comic computer concert cook cookie
cool corner could country cousin cover cow cry culture cup curry cut cute dance danger dangerous dark date
daughter day dead dear December decide deep delicious department desk dictionary die different difficult
dinner dirty do doctor dog doll dollar door down draw dream dress drink drive driver drop dry during each ear
early earth easily east easy eat egg eight either elementary elephant else email end energy engineer English
enjoy enough enter environment eraser especially even evening event ever every everyone everything example
excited exciting excuse eye face fact factory fall family famous fan far farm farmer fast father favorite
February feel festival few field fight find fine finger finish fire first fish five floor flower fly follow
food foot for foreign forest forget fork four free Friday friend friendly friendship from front fruit full
fun funny future game garden gate get gift girl give glad glass go goal good grandfather grandmother great
green ground group grow guess guest guitar hair half hall hamburger hand happen happy hard hat have he head
health healthy hear heart heavy hello help her here hers high hike hill him his history hobby hold holiday
home homework hope horse hospital hot hotel hour house how however human hundred hungry hurry hurt husband I
ice idea if important in information inside interested interesting international into introduce invite is
island it its January job join joy juice July jump June junior just keep key kid kill kind king kitchen knife
know lake language large last late later laugh lead leader learn least leave left leg lend less lesson let
letter library lie life light like line lion list listen little live local long look lose lot loud love low
lucky lunch machine make man many map March market marry match math matter may May maybe me meal mean meat
medicine meet meeting member memory message middle milk million mind minute miss mistake Monday money month
moon more morning most mother mountain mouse mouth move movie much museum music must my name nature near need
neighbor never new news newspaper next nice night nine no noise noon north nose not note notebook nothing
notice November now number nurse o'clock October of off office often oh old on once one only open or orange
other our out outside over own page paint paper parent park part party pass past pay peace pen pencil people
perfect person pet phone piano pick picture piece pig place plan plane planet plant play player please pocket
point police poor popular possible post potato power practice prepare present pretty price problem project
proud pull push put question quick quickly quiet quietly rabbit race rain rainy read ready real really reason
receive red remember report rest restaurant return rice rich ride right river road robot rock room rule run
sad safe same sandwich Saturday save say school science scientist sea season seat second see sell send
September serve set seven shall she ship shirt shoe shop short should shoulder shout show shy sick side sign
simple since sing singer sister sit six size skate ski skill sky sleep slow slowly small smell smile snow so
soccer social soft some someone something sometimes son song soon sorry sound soup south space speak speaker
special speech spend sport spring stadium stage stand star start station stay step still stomach stop store
story strange street strong student study subject such sugar summer sun Sunday supper sure surprise surprised
swim system table take talk tall taste tea teach teacher team tell temple ten tennis terrible test than thank
that the theater their them then there these they thing think third thirsty this those thousand through
throw Thursday ticket time tired to today together tomorrow tonight too tooth top touch tour tourist towel town
toy traditional traffic train travel tree trip trouble true try Tuesday turn TV twice two umbrella uncle under
understand uniform until up us use useful usually vacation vegetable very village visit visitor voice
volunteer wait wake walk wall want war warm wash watch water way we weak wear weather Wednesday week weekend
welcome well west wet what when where which while white who whole whose why wide wife will win wind window
winter wish with without woman wonder wonderful wood word work world worry would write wrong year yellow yes
yesterday yet you young your zoo
""".lower().split())
