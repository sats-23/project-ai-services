import React, { useState } from "react";
import { CustomResponseExample } from "./CustomResponseExample.jsx";
import { ReferenceDocsButton } from "./ReferenceDocsButton.jsx";

function renderUserDefinedResponse(state, instance) {
  const { messageItem } = state;
  const type = messageItem?.user_defined?.user_defined_type;

  switch (type) {
    case "my_unique_identifier":
      return <CustomResponseExample data={messageItem.user_defined} />;
    case "reference_docs_button":
      return <ReferenceDocsButton data={messageItem.user_defined} />;
    default:
      return undefined;
  }
}

export { renderUserDefinedResponse };
