Human decision gates gained explicit comment actions: «Одобрить с
комментарием» records an accept with the operator's free text, stage gates
offer «Вернуть на доработку с комментарием» (revise, feedback required), and
plain «Одобрить»/«Отклонить» always send the bare decision. proposed_check
answers now journal the operator's comment in the HitlDecided event too
(additive `feedback` key; events without it keep the historical contract).
Revise/reject feedback re-drives the stage attempt through the repair path;
accepted (accept/approve) feedback now also rides forward as human guidance:
it is folded from the journal and passed as an extra user message
(«Указание человека для дальнейшей работы: …») into the later planner /
executor-route / hypothesizer / researcher / tz_spec role runs — a direction
for the work going forward, never a repair of the accepted stage. Runs whose
journal has no accepted comments behave exactly as before.
