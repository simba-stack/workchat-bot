module.exports = {
  packagerConfig: {
    name: "PRIDE J.A.R.V.I.S.",
    executableName: "pride-jarvis",
    asar: true,
    icon: "./icon",
    appBundleId: "com.pride.jarvis",
    appCategoryType: "public.app-category.business",
  },
  rebuildConfig: {},
  makers: [
    {
      name: "@electron-forge/maker-squirrel",
      config: {
        name: "PRIDE-JARVIS",
        setupIcon: "./icon.ico",
        iconUrl: "https://workchat-bot-production.up.railway.app/favicon.ico",
      },
    },
    {
      name: "@electron-forge/maker-zip",
      platforms: ["darwin", "linux", "win32"],
    },
    {
      name: "@electron-forge/maker-dmg",
      config: { format: "ULFO" },
    },
    {
      name: "@electron-forge/maker-deb",
      config: {
        options: {
          maintainer: "SIMBA",
          homepage: "https://workchat-bot-production.up.railway.app",
        },
      },
    },
  ],
  publishers: [
    {
      // GitHub Releases — куда выкладываются новые версии
      // и откуда electron-updater будет их скачивать.
      name: "@electron-forge/publisher-github",
      config: {
        repository: {
          owner: "simba-stack",
          name: "workchat-bot",
        },
        prerelease: false,
        draft: false,
        // Для авто-публикации нужно env: GITHUB_TOKEN с правами repo
      },
    },
  ],
  plugins: [],
};
