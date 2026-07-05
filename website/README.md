# MyBestTeam — Marketing Website

Static Astro site. No runtime dependency on `ui/backend/`.

## Local dev

```
cd website && npm install && npm run dev   # http://localhost:4321
```

## Build

```
npm run build   # output in website/dist/
```

## Deploy

- **Vercel**: import the repo, set root directory to `website/`.
- **Netlify**: set base directory to `website/`, build command `npm run build`, publish dir `dist`.
