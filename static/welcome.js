"use strict";
(() => {
  const button=document.getElementById("start-analysis");
  const status=document.getElementById("start-status");
  const label=button.querySelector(".button-label");
  let starting=false;
  async function fetchLocal(path,options={}){
    const controller=new AbortController();
    const timer=window.setTimeout(()=>controller.abort(),10000);
    try{
      const response=await fetch(path,{cache:"no-store",credentials:"same-origin",...options,signal:controller.signal});
      if(!response.ok)throw new Error("request_failed");
      return await response.json();
    }finally{window.clearTimeout(timer);}
  }
  button.addEventListener("click",async()=>{
    if(starting)return;
    starting=true;button.disabled=true;button.setAttribute("aria-busy","true");
    label.textContent="분석 준비 중";status.classList.remove("error");status.textContent="이 컴퓨터의 분석기에 연결하고 있습니다.";
    try{
      const health=await fetchLocal("/api/health");
      if(health.app_id!=="resource-analyzer"||typeof health.csrf_token!=="string"||!health.csrf_token)throw new Error("app_unavailable");
      status.textContent="자원 사용량 측정을 시작하고 있습니다.";
      await fetchLocal("/api/monitor/start",{method:"POST",headers:{"Content-Type":"application/json","X-Resource-Analyzer-Token":health.csrf_token},body:"{}"});
      window.location.assign("/dashboard");
    }catch(error){
      status.classList.add("error");
      status.textContent=error.name==="AbortError"?"분석기 응답이 지연되고 있습니다. 잠시 후 다시 눌러 주세요.":"분석기에 연결하지 못했습니다. 자원 분석기 실행 파일이 열려 있는지 확인한 뒤 다시 눌러 주세요.";
      starting=false;button.disabled=false;button.removeAttribute("aria-busy");label.textContent="내 컴퓨터 분석하기";
    }
  });
})();
