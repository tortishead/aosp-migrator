A web-first tool with AI agent support for automobile OEM to migrate their changes in AOSP from one Android version (source project) to another (target project).

## What user have
 - AOSP source tree of target project, clean built
 - AOSP source tree of source project (optional)
 - A list of applied patches in source project as Jira ticket, which contains
    - description of the patch
    - diff file as attatchmen
    - links to the implementation tickets of srouce project (optional)
 - Access to GH Copilot 
 - required PAT of GH/Jira etc. 

## Guiding principle
 - AI credit is limited, save token as much as possible 
 
## Web front end
- Each migraton project is a workspace with backlogs
- For each work item, user can see the details and working history
- Contains settings panel

## Module 1 Import
- User import working items from Jira via REST-API from a JQL filter
- After import, working items are visible in the workspace as a backlog with all relevant information
- User can 
    - filter the backlog
    - mark them as not relevant or close them
    - add notes
    - reimport
    - start different migration level

## Pre-Pocessing
Pre processing should do the following with GH copilit:
- use all available information from Jira and code to understand the purpase patch. 
- identify affected repo/project
- verify if the patch makes sense, is still required/relevant
- Very often, pathes rely on each other and need to be migrated as cluster. Pre Process should understand the dependency between patches and cluster them.
- The cluster of tickets should be reflected in the web UI

## Migration - level 1
Can apply plain patch, go level 2 if failed

## Migration - level 2
Can merged with e.g. 3way or other strategy. 
Normally no copilot support is needed
Go level 3 if failed

## Migration - level 3
New implementation from Copilot, which should archive the same as original patch
Migration task failed if failed.

## Build and verify
After each migration try, build (only) affected modules. 
Test (e.g. affected CTS tests) can be executed as optional. 
Migration is done when build and optional tests are successful

## Human review
All migrated tickets needs to be reviewed by human

## Push 
The result will be pushed to jira as comment/github as commit in a user defined migration branch and PR 
This action is triggeered by user for all or selected items 