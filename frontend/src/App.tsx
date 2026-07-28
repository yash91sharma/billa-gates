import { Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import { TooltipProvider } from './components/ui/tooltip'
import Dashboard from './pages/Dashboard'
import JobDetail from './pages/JobDetail'
import Jobs from './pages/Jobs'
import RunDetail from './pages/RunDetail'
import Settings from './pages/Settings'

export default function App() {
  // One tooltip provider for the whole app. `TriggeredByIcon` used to bring
  // its own, which meant a provider per table row — twenty of them on a busy
  // dashboard, each with its own independent hover-delay state.
  return (
    <TooltipProvider delayDuration={200}>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/jobs/:id" element={<JobDetail />} />
          <Route path="/runs/:id" element={<RunDetail />} />
          <Route path="/settings" element={<Settings />} />
        </Route>
      </Routes>
    </TooltipProvider>
  )
}
