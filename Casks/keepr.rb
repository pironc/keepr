cask "keepr" do
  arch arm: "aarch64", intel: "x86_64"

  version :latest
  sha256 :no_check

  url "https://github.com/pironc/keepr/releases/latest/download/keepr-mac-#{arch}.dmg"
  name "keepr"
  desc "Privacy-first, local-first document RAG chat assistant"
  homepage "https://github.com/pironc/keepr"

  app "keepr.app"
end
