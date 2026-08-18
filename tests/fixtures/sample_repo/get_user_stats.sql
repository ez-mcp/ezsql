-- Get active users with their order totals
SELECT u.id, u.username, u.email, COUNT(o.id) as order_count, SUM(o.total) as total_spent
FROM users u
LEFT JOIN orders o ON u.id = o.user_id
WHERE u.username IS NOT NULL
GROUP BY u.id, u.username, u.email
ORDER BY total_spent DESC;
