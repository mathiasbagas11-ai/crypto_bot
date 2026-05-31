import streamlit as st
import pathlib

st.set_page_config(
    page_title="CryptoBot v13",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Hide Streamlit default chrome
st.markdown("""
<style>
  #MainMenu, header, footer { visibility: hidden; }
  .block-container { padding: 0 !important; max-width: 100% !important; }
  [data-testid="stAppViewContainer"] { background: #080b10; }
</style>
""", unsafe_allow_html=True)

html_content = pathlib.Path(__file__).parent / "index.html"
st.components.v1.html(html_content.read_text(encoding="utf-8"), height=6000, scrolling=True)
