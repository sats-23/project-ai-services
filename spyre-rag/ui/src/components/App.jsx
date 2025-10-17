import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import {
  ChatContainer,
  ChatCustomElement,
  BusEventType,
  FeedbackInteractionType,
  CornersType,
  MinimizeButtonIconType,
  PublicConfig,
} from "@carbon/ai-chat";
import "./App.scss"
import HeaderNav from "./Header.jsx"
import { Theme, Content, Grid, Column } from "@carbon/react";
import { customLoadHistory } from "./customLoadHistory.js";
import { customSendMessage } from "./customSendMessage_2.jsx";
import { renderUserDefinedResponse } from "./renderUserDefinedResponse.js";

const config = {
  messaging: {
    customSendMessage,
    // customLoadHistory
  },
  headerConfig: {
    hideMinimizeButton: true,
    minimizeButtonIconType: undefined,
  },
  themeConfig: {
    corners: CornersType.SQUARE,
  },
  layout: {
    // showFrame: false,
    hasContentMaxWidth: false,
  },
  openChatByDefault: true,
};

function App() {
  const [chatInstance, setChatInstance] = useState();

  function onBeforeRender(instance) {

    instance.updateMainHeaderAvatar({
      source: 'https://isv-graphics.s3.us-south.cloud-object-storage.appdomain.cloud/PAC-background-new.jpg',
      corners: 'round',
    });
    instance.updateMainHeaderTitle('FarmerAI');

    // instance.updateCSSVariables({
    //   '--cds-chat-shell-background:': '#0f62fe',
    // });

    instance.on({ type: BusEventType.FEEDBACK, handler: feedbackHandler });
    setChatInstance(instance);

    instance.messaging.addMessage({
      output: {
        generic: [
          {
            response_type: "text",
            text: `Hi, I'm your assistant! You can ask me anything related to your crop, pesticide, fertilizer, etc.
    नमस्ते, मैं आपका सहायक हूँ! आप मुझसे अपनी फसल, कीटनाशक, उर्वरक आदि से संबंधित कोई भी प्रश्न पूछ सकते हैं।`,
          },
        ],
      },
    });
  }

  function feedbackHandler(event) {
    if (event.interactionType === FeedbackInteractionType.SUBMITTED) {
      const { message, messageItem, ...reportData } = event;
      setTimeout(() => {
        window.alert(JSON.stringify(reportData, null, 2));
      });
    }
  }


  return (
      <React.Fragment>
        <Theme theme="white">
          <Content id="main-content">
            <Grid fullWidth className="chat-page-grid">
              <Column sm={4} md={8} lg={12}>
                <Theme theme="g90"> 
                  <HeaderNav />    
                </Theme>
              </Column>
              <Column sm={4} md={8} lg={12}>
                <div className="chat-container">
                  <ChatCustomElement
                    config={config}
                    className="fullScreen"
                    onBeforeRender={onBeforeRender}
                    renderUserDefinedResponse={renderUserDefinedResponse}
                  />
                </div>
              </Column>
            </Grid>
          </Content>
        </Theme>
      </React.Fragment>
  );
}

export default App;
// const root = createRoot(document.querySelector("#root"));
// root.render(<App />);
