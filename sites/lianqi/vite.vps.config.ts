import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
import {fileURLToPath} from 'node:url';
export default defineConfig({
 root:fileURLToPath(new URL('./vps',import.meta.url)),
 base:'/site/',
 publicDir:false,
 plugins:[react()],
 css:{postcss:fileURLToPath(new URL('.',import.meta.url))},
 build:{outDir:fileURLToPath(new URL('../../public/site',import.meta.url)),emptyOutDir:true},
});
