# Universal Emotes Packs

This repository contains list of community created Bundle Packs. Once you add your repository here, it will save your bundle file to cloud. Workflow every hour reads latest releases of repositories on sources and updates this repository.

## How to create Bundle for Universal Emotes mod and about rules?
Visit main page of [Universal Emotes](https://github.com/theptah/universal-emotes) by clicking here.

## How to release Bundle?

1. Create your own **public** repository for bundle pack.
> **NOTE:** The repository must stay public and belong to your account. If it belongs to an organization, you must be a member and your membership must be public (organization page → People). If the repository is **deleted**, **made private** or **transferred to another account**, it **won't be removed** from catalog. For permanent deletion file from cloud, you need to open pull request for deletion of your source file (`sources/<your_github_username>.json`) or if you have more repositories and want some repos to delete, you can just remove the repo in source file.

2. Then create a release that you make sure attach a `.zip` file for each bundle in your repo. 

> **NOTE:** Make sure it is a **full release**, drafts and pre-releases will be **ignored**. The Source code (zip) that GitHub adds to every release automatically does not count. 

> **NOTE:** You can only attach **10 files per release** AND **REMEMBER!** If you create a new release without re-attaching the previous bundle files, they will be removed from the catalog. Always include previous bundle files when publishing a new one.

3. After that fork this repository, then make sure you are on the page of your forked repository otherwise you won't have access for next step.

> **NOTE**: If you already have a fork from before, navigate to `Sync fork → Update branch` on your fork's page first, so your copy is up to date before you edit anything.

4. Now navigate to `Add file -> Create new file`.
5. After redirection, Click the `Name your file..` field then type `sources/<your_github_username>.json` (for example: `sources/theptah.json`).

> **NOTE:** Make sure you typed your github username in **small letters (lowercase)** otherwise its not allowed!

6. After that click the `Enter file content here` field and write this json data in there
```
{
  "repos": [
    "<your_github_username>/<your_repository_name>"
  ]
}
```
In the future, you can add your new bundle repositories to this `repos` array.
**For example;**
```
{
  "repos": [
    "theptah/universal-emotes-officials",
    "theptah/universal-emotes-officials-newone"
  ]
}
```

> **WARN**: The file must contain only the repos key, every repository must be written as `<owner>/<repo_name>` format, the same repository can't be listed twice, a file can list at most 100 repositories and the file can be at most 8 KB. A repository can be listed only once in the whole catalog.

7. Click `Commit changes` button then confirm the commit in the modal that appears.
8. Once you redirected to fork's page, navigate to `Contribute -> Open pull request`.
9. Add your lovely descriptions (optional), then click `Create pull request`. Wait for the automated checks to pass. If it fails, the bot posts reason as a comment on your pull request, review the reason and update your repository/commit by addressing the reported errors.
10. THAT'S IT, now your bundle pack released in our catalog. 

### Did you change your GitHub username?
No problem! Your file is tied to your GitHub account, not to your username, so you don't lose it:

- You can keep editing your old file from your renamed account.
- You can move it to your new name just by renaming the file to `sources/<new_username>.json` in a pull request and changing repo names in it.
- Somebody who later registers your old username can't edit or delete your file.
