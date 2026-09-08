import React from 'react';
import {createRoot} from 'react-dom/client';
import {LianqiDashboard} from '../app/lianqi-dashboard';
import '../app/globals.css';
createRoot(document.getElementById('root')!).render(<React.StrictMode><LianqiDashboard /></React.StrictMode>);
