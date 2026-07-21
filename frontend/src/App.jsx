import { Route, Routes } from 'react-router-dom'
import MarketingLayout from './components/MarketingLayout'
import ProtectedRoute, { AdminRoute, UserRoute } from './components/ProtectedRoute'
import ConnectivityBanner from './components/ConnectivityBanner'
import Landing from './pages/Landing'
import Login from './pages/Login'
import Register from './pages/Register'
import VerifyEmail from './pages/VerifyEmail'
import ResetPassword from './pages/ResetPassword'
import Dashboard from './pages/Dashboard'
import NewAnalysis from './pages/NewAnalysis'
import History from './pages/History'
import Report from './pages/Report'
import Admin from './pages/Admin'
import Settings from './pages/Settings'

function App() {
  return (
    <>
      <ConnectivityBanner />
      <Routes>
        <Route element={<MarketingLayout />}>
          <Route path="/" element={<Landing />} />
          <Route path="/login" element={<Login />} />
          <Route path="/register" element={<Register />} />
        </Route>

        <Route path="/verify-email" element={<VerifyEmail />} />
        <Route path="/reset-password" element={<ResetPassword />} />

        <Route element={<ProtectedRoute />}>
          <Route element={<UserRoute />}>
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/analyze" element={<NewAnalysis />} />
            <Route path="/history" element={<History />} />
            <Route path="/report/:id" element={<Report />} />
          </Route>
          <Route path="/settings" element={<Settings />} />
          <Route element={<AdminRoute />}>
            <Route path="/admin" element={<Admin />} />
          </Route>
        </Route>
      </Routes>
    </>
  )
}

export default App
