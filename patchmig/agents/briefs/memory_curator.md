# Task: propose lessons for the migration memory

We migrate an automotive OEM's Android patches from Android {source_version} to Android
{target_version} with an agent per patch cluster. Below is what went wrong or needed a human
in this run: reviewer feedback, human decisions, build fixes and review concerns. Turn it
into a few short, reusable lessons that would help the agent next time (in later clusters
and in the next migration), for example "In CarPowerService, Android 15 passes a reason to
setMode(); OEM callers use REASON_OEM", or "Overlays under vendor/oem/overlays replace
config.xml edits; never edit core/res for OEM defaults".

Rules:
- Only lessons supported by the signals below; name the clusters they come from.
- Concrete and specific beats general. No lessons that just restate a single fix.
- Give `applies_to` globs over tree paths (e.g. `frameworks/base/core/java/android/os/**`)
  so the lesson reaches only the clusters it concerns.
- At most {max_lessons} lessons. Skip any that repeats an existing lesson. No lessons at all
  is a fine answer.
- Do not run commands or edit files.

## Existing lessons

{existing}

## Signals from this run

{signals}
