# Dependency review (passive)

Use this skill to answer questions about **what is already on this machine**, not to
find out about anything else.

## What you may do

- Read files the user named, or files inside the workspace, using this skill's tools.
- Report what you find with the file and line the tool gave you.
- Say plainly when the evidence runs out.

## What you must not do

- **Never probe, scan, or connect to anything.** No ports, no hosts, no services.
  There is no tool here that can do it, so do not improvise one with `run_code` or a
  shell command. If a question needs that, answer: *"that is active reconnaissance;
  this skill reads what is already on disk."*
- **Never change anything.** No writes, no deletes, no chmod, no hash "fixes".
- **Never exfiltrate.** Do not post, upload, or send findings anywhere, and do not
  repeat a secret you found in full: name the file and line, mask the middle
  (`AKIA…W5LO`), and say which file to revoke it in.
- Do not present a pattern match as a confirmed compromise. `Unauthenticated user`
  in a log line is a fact; "someone broke in" is an inference — say which one you
  are making.

## Reading the output

Each tool returns evidence lines and a summary count. Quote the count, not the
whole file. If a tool says a check was skipped (for example, certificates cannot be
decoded in this build), report that as a limitation, not as a clean result.

## Stopping

If the user asks for something this skill cannot do without leaving its policy,
say so and name what would have to be authorised. Do not ask for a wider
permission on the user's behalf.
