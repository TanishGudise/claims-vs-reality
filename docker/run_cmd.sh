#!/bin/bash
# Runs the agent's command (from $CVR_CMD) as the traced unprivileged user, then
# SIGKILLs every leftover process of that user so no background job outlives the
# call. `kill -KILL -1` skips the calling shell itself.
cd /work 2>/dev/null
eval "$CVR_CMD"
ec=$?
exec 1>&- 2>&-
kill -KILL -1 2>/dev/null
exit $ec
