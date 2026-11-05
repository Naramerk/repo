A json_pointer check with pointer `/` now evaluates the whole registered
document (root presence) instead of refusing with `pointer_empty`. RFC 6901
reads `/` as the empty-string key, which no receipt object carries, so the
v6 `research_result_payload` presence check («указатель на корень артефакта»)
failed on every run regardless of content — required-validity FAILED made
report tasks PARTIAL and kept artifact questions `rubric_not_met` (live
№25 → №29). The null/empty guard below the pointer read still judges
presence honestly: a registered JSON document METs, a null document refuses.
