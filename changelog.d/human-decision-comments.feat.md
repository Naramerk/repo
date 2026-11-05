Human decision gates gained explicit comment actions: «Одобрить с
комментарием» records an accept with the operator's free text, stage gates
offer «Вернуть на доработку с комментарием» (revise, feedback required), and
plain «Одобрить»/«Отклонить» always send the bare decision. proposed_check
answers now journal the operator's comment in the HitlDecided event too
(additive `feedback` key; events without it keep the historical contract).
Revise/reject feedback still re-drives the stage attempt through the repair
path; accept feedback stays journal/report-only.
