# Publishing to a Launchpad PPA

A PPA takes **source** packages, not `.deb` files: you upload a signed
`_source.changes` and Launchpad builds the binary itself, once per Ubuntu
series. The `debian/` directory in this repository is what makes that
possible — `packaging/build-deb.sh` stays around for building a `.deb`
locally or in CI, and both install the very same files from
`packaging/files/`.

The examples below use `ppa:stzahi/logi-mx4-ring`; substitute your own.

## Once, per machine

```
sudo apt install debhelper devscripts dput
```

## Once, per Launchpad account

Uploads must be signed by a key Launchpad knows, so:

```
gpg --full-generate-key            # RSA 4096, your name and the email on the account
gpg --list-secret-keys --keyid-format=long   # note the KEYID after "sec rsa4096/"
gpg --send-keys --keyserver keyserver.ubuntu.com <KEYID>
```

Then paste the fingerprint into <https://launchpad.net/~/+editpgpkeys>.
Launchpad replies with an encrypted mail; decrypt it (`gpg --decrypt`) and open
the link inside to confirm the key.

## Every upload

The version has to be new and to name the series it targets, so
`debian/changelog` gets an entry per series — `0.6.0~resolute1` for resolute,
`0.6.0~noble1` for noble, and so on. `dch` writes them:

```
dch -v 0.6.0~resolute1 --distribution resolute "What changed"
```

Then build the source package, signing it with the key Launchpad has, and send
it:

```
debuild -S -sa -k<KEYID>
dput ppa:stzahi/logi-mx4-ring ../mx4ctl_0.6.0~resolute1_source.changes
```

Launchpad mails you when the build finishes (a few minutes), and rejects the
upload outright if the version already exists — bump the trailing number.

To cover another series, either repeat with that series' version, or use
**Copy packages** in the PPA's web interface with *Rebuild the copied
packages* ticked; the package is `Architecture: all`, so nothing else changes.

## Installing from the PPA

Once a build has published, on any Ubuntu machine:

```
sudo add-apt-repository ppa:stzahi/logi-mx4-ring
sudo apt update
sudo apt install mx4ctl
```

From then on `apt search mx4ctl` finds it and `apt upgrade` picks up new
uploads. Getting into `apt search` *without* adding the PPA would mean
entering the Ubuntu archive proper, which goes through Debian and a sponsor
rather than a PPA.
