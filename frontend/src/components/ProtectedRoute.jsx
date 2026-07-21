import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../lib/AuthContext'
import AppNav from './AppNav'
import ChatWidget from './ChatWidget'

export default function ProtectedRoute() {
  const { user, isAdmin, initializing } = useAuth()
  const location = useLocation()

  if (initializing) return <div className="page-loading">Loading...</div>
  if (!user) return <Navigate to="/login" state={{ from: location }} replace />

  return (
    <>
      <AppNav />
      <Outlet />
      {!isAdmin && <ChatWidget />}
    </>
  )
}

export function AdminRoute() {
  const { isAdmin, initializing } = useAuth()

  if (initializing) return <div className="page-loading">Loading...</div>
  if (!isAdmin) return <Navigate to="/dashboard" replace />

  return <Outlet />
}

// Admin accounts only get the Admin dashboard — regular user pages (Dashboard,
// New Analysis, History, Report) bounce them back to /admin.
export function UserRoute() {
  const { isAdmin, initializing } = useAuth()

  if (initializing) return <div className="page-loading">Loading...</div>
  if (isAdmin) return <Navigate to="/admin" replace />

  return <Outlet />
}
