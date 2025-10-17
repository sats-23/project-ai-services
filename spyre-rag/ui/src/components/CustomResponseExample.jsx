import React, { useEffect, useState } from "react";
import "./CustomResponseStyles.css";

function CustomResponseExample({ data }) {
  const [timestamp, setTimestamp] = useState(0);

  useEffect(() => {
    const interval = setInterval(() => setTimestamp(Date.now()), 1000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="external">
      This is a user_defined response type with external styles. The following
      is some text passed along for use by the back-end: {data.text}. And here
      is a value being set by state: {timestamp}.
    </div>
  );
}

export { CustomResponseExample };
