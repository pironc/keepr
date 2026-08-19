cask "keepr" do
  arch arm: "aarch64", intel: "x86_64"

  version :latest
  sha256 :no_check

  url "https://github.com/pironc/keepr/releases/latest/download/keepr-mac-#{arch}.dmg"
  name "keepr"
  desc "Privacy-first, local-first document RAG chat assistant"
  homepage "https://github.com/pironc/keepr"

  app "keepr.app"

  caveats <<~EOS
    keepr isn't code-signed or notarized yet, so macOS will say the app
    "is damaged and can't be opened" on first launch. This clears the
    quarantine flag rather than actually repairing anything broken:
      xattr -cr #{appdir}/keepr.app
  EOS
end
